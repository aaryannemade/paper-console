"""ESP32-S3 relay simulator: the device side of docs/relay-protocol.md.

Stands in for the firmware (Step 6) during development and tests. It sends
heartbeats to the container, accepts pushed print jobs, "writes" each record to a
simulated UART at the configured baud rate, decodes the raster data back into the
receipt image (saved as PNG) and reports job status.

  PC1_RELAY_TOKEN=... python testing/relay_simulator.py                 # 9600 baud
  PC1_RELAY_TOKEN=... python testing/relay_simulator.py --baud 0        # instant
  python testing/relay_simulator.py --server http://127.0.0.1:8000 --port 8090 \\
      --out testing/artifacts/relay --token "$PC1_RELAY_TOKEN"

The firmware must pass the same protocol behavior this simulator implements.
"""

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.drivers import escpos  # noqa: E402

logger = logging.getLogger("relay_simulator")

FINAL = {"completed", "cancelled", "failed"}
JOB_PATH = re.compile(r"^/jobs/(?P<id>[A-Za-z0-9_-]{1,64})(?P<rest>/.*)?$")
CHUNK_PATH = re.compile(r"^/chunks/(?P<index>\d{1,6})$")


class SimJob:
    def __init__(self, job_id, kind, chunks, records, total_bytes):
        self.job_id = job_id
        self.kind = kind
        self.expected_chunks = chunks
        self.expected_records = records
        self.expected_bytes = total_bytes
        self.received_chunks = 0
        self.received_records = 0
        self.received_bytes = 0
        self.queue = deque()
        self.queued_bytes = 0
        self.records_written = 0
        self.bytes_written = 0
        self.data = bytearray()
        self.state = "receiving"
        self.error = None
        self.cancel = False
        self.created = time.monotonic()
        self.finished = None

    def status(self, boot_id):
        return {
            "job_id": self.job_id,
            "state": self.state,
            "boot_id": boot_id,
            "records_written": self.records_written,
            "bytes_written": self.bytes_written,
            "error": self.error,
        }


class RelaySimulator:
    def __init__(
        self,
        server_url,
        token,
        *,
        host="127.0.0.1",
        port=0,
        baud=9600,
        out_dir=None,
        device_id="sim-esp32",
        firmware="simulator",
        max_chunk_bytes=8192,
        max_job_bytes=1024 * 1024,
        buffer_limit_bytes=None,
        report_status=True,
        heartbeat=True,
        heartbeat_interval=None,
        advertise_url=None,
    ):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.host = host
        self.port = port
        self.baud = baud
        self.out_dir = Path(out_dir) if out_dir else None
        self.device_id = device_id
        self.firmware = firmware
        self.max_chunk_bytes = max_chunk_bytes
        self.max_job_bytes = max_job_bytes
        self.buffer_limit_bytes = buffer_limit_bytes
        self.report_status = report_status
        self.heartbeat_enabled = heartbeat
        self.heartbeat_interval = heartbeat_interval
        self.advertise_url = advertise_url
        self.printer_state = "ready"
        self.printer_detail = None
        self.boot_id = uuid.uuid4().hex[:12]

        self.cond = threading.Condition()
        self.jobs: "OrderedDict[str, SimJob]" = OrderedDict()
        self.active: SimJob = None
        self.receipts = []  # (job_id, PIL image) of completed/cancelled receipts
        self.status_posts = []
        self.heartbeats_sent = 0
        self.last_heartbeat_error = None
        self._stop = threading.Event()
        self._threads = []
        self._session = requests.Session()
        self.httpd = None

    # --- lifecycle -------------------------------------------------------------

    @property
    def url(self):
        return self.advertise_url or f"http://{self.host}:{self.port}"

    def start(self):
        self._stop.clear()
        self.httpd = ThreadingHTTPServer((self.host, self.port), self._handler_class())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        for target in (lambda: self.httpd.serve_forever(poll_interval=0.05), self._uart_loop, self._heartbeat_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self):
        self._stop.set()
        with self.cond:
            self.cond.notify_all()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads = []

    def reboot(self):
        """Simulate a power cycle: new boot id, all job state lost."""
        with self.cond:
            self.boot_id = uuid.uuid4().hex[:12]
            self.jobs.clear()
            self.active = None
            self.cond.notify_all()
        self.send_heartbeat()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # --- device -> container -----------------------------------------------------

    def _post(self, path, payload):
        return self._session.post(
            f"{self.server_url}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=5,
        )

    def send_heartbeat(self):
        with self.cond:
            active = self.active.job_id if self.active and self.active.state not in FINAL else None
            payload = {
                "device_id": self.device_id,
                "boot_id": self.boot_id,
                "firmware": self.firmware,
                "callback_url": self.url,
                "printer": {
                    "state": self.printer_state,
                    "detail": self.printer_detail,
                    "width_dots": 384,
                    "baudrate": self.baud or 9600,
                },
                "limits": {
                    "max_chunk_bytes": self.max_chunk_bytes,
                    "max_job_bytes": self.max_job_bytes,
                },
                "active_job_id": active,
            }
        try:
            response = self._post("/api/device/heartbeat", payload)
            response.raise_for_status()
            self.heartbeats_sent += 1
            self.last_heartbeat_error = None
            return response.json()
        except Exception as exc:
            self.last_heartbeat_error = str(exc)
            return None

    def _heartbeat_loop(self):
        while not self._stop.is_set():
            interval = 2.0
            if self.heartbeat_enabled:
                reply = self.send_heartbeat()
                if reply:
                    interval = self.heartbeat_interval or reply.get("heartbeat_interval_s", 10)
            self._stop.wait(interval)

    def _report(self, job):
        if not self.report_status:
            return
        payload = job.status(self.boot_id)
        self.status_posts.append(dict(payload))
        try:
            self._post(f"/api/device/jobs/{job.job_id}/status", payload)
        except Exception:
            logger.debug("status report failed", exc_info=True)

    # --- simulated UART ----------------------------------------------------------

    def _uart_loop(self):
        while not self._stop.is_set():
            with self.cond:
                job = self.active
                record = None
                if job is not None and job.state not in FINAL:
                    if job.cancel:
                        self._finish_locked(job, "cancelled")
                    elif job.queue:
                        record = job.queue.popleft()
                        job.queued_bytes -= len(record)
                        if job.state == "receiving":
                            job.state = "printing"
                    elif job.received_chunks == job.expected_chunks:
                        self._finish_locked(job, "completed")
                if record is None:
                    self.cond.wait(0.05)
                    continue
            # "Write" outside the lock so the HTTP server stays responsive.
            if self.baud:
                time.sleep(len(record) * 10 / self.baud)
            with self.cond:
                job.data += record
                job.records_written += 1
                job.bytes_written += len(record)
                self.cond.notify_all()
            if job.records_written == 1:
                self._report(job)

    def _finish_locked(self, job, state, error=None):
        job.state = state
        job.error = error
        job.finished = time.monotonic()
        self.cond.notify_all()
        if job.kind == "receipt" and state in ("completed", "cancelled"):
            image = escpos.decode_raster(bytes(job.data))
            if image is not None:
                upright = image.rotate(180)
                self.receipts.append((job.job_id, upright))
                saved = ""
                if self.out_dir:
                    self.out_dir.mkdir(parents=True, exist_ok=True)
                    path = self.out_dir / f"relay-{time.strftime('%Y%m%d-%H%M%S')}-{job.job_id[:8]}.png"
                    upright.save(path)
                    saved = f", saved {path}"
                print(
                    f"[SIM] receipt {job.job_id[:8]} {state}: {image.width}x{image.height} dots, "
                    f"{job.records_written} records, {job.bytes_written} bytes, "
                    f"{job.finished - job.created:.1f}s{saved}",
                    flush=True,
                )
        elif job.kind != "receipt":
            print(f"[SIM] {job.kind} {job.job_id[:8]} {state} ({job.bytes_written} bytes)", flush=True)
        threading.Thread(target=self._report, args=(job,), daemon=True).start()

    # --- container -> device HTTP API --------------------------------------------------

    def _handler_class(sim):  # noqa: N805 - closure over the simulator
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # quiet
                logger.debug(fmt, *args)

            def _send(self, code, payload=None, headers=None):
                body = json.dumps(payload or {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _authorized(self):
                if self.headers.get("Authorization") == f"Bearer {sim.token}":
                    return True
                self._body()
                self._send(401, {"error": "unauthorized"})
                return False

            def do_GET(self):
                if not self._authorized():
                    return
                if self.path == "/status":
                    return self._send(200, {"device_id": sim.device_id, "boot_id": sim.boot_id})
                match = JOB_PATH.match(self.path)
                if not match or match.group("rest"):
                    return self._send(404, {"error": "not found"})
                with sim.cond:
                    job = sim.jobs.get(match.group("id"))
                    if job is None:
                        return self._send(404, {"error": "unknown job"})
                    return self._send(200, job.status(sim.boot_id))

            def do_POST(self):
                if not self._authorized():
                    return
                body = self._body()
                if self.path == "/jobs":
                    return self._create(body)
                match = JOB_PATH.match(self.path)
                if match and match.group("rest") == "/cancel":
                    with sim.cond:
                        job = sim.jobs.get(match.group("id"))
                        if job is None:
                            return self._send(404, {"error": "unknown job"})
                        if job.state not in FINAL:
                            job.cancel = True
                            sim.cond.notify_all()
                        return self._send(200, job.status(sim.boot_id))
                self._send(404, {"error": "not found"})

            def do_PUT(self):
                if not self._authorized():
                    return
                body = self._body()
                match = JOB_PATH.match(self.path)
                chunk = CHUNK_PATH.match(match.group("rest") or "") if match else None
                if not chunk:
                    return self._send(404, {"error": "not found"})
                index = int(chunk.group("index"))
                with sim.cond:
                    job = sim.jobs.get(match.group("id"))
                    if job is None:
                        return self._send(404, {"error": "unknown job"})
                    if index < job.received_chunks:
                        return self._send(200, {"received": index, "duplicate": True})
                    if index != job.received_chunks:
                        return self._send(409, {"error": "out of order", "expected": job.received_chunks})
                    if job.state in FINAL:
                        return self._send(409, {"error": f"job {job.state}"})
                    if len(body) > sim.max_chunk_bytes:
                        return self._send(413, {"error": "chunk too large"})
                    final = index == job.expected_chunks - 1
                    if self.headers.get("X-PC1-Final-Chunk") != ("1" if final else "0"):
                        return self._send(400, {"error": "final chunk flag mismatch"})
                    if (
                        sim.buffer_limit_bytes is not None
                        and job.queue
                        and job.queued_bytes + len(body) > sim.buffer_limit_bytes
                    ):
                        return self._send(429, {"error": "buffer full"}, {"Retry-After-Ms": "50"})
                    try:
                        records = escpos.parse_framed_records(body)
                    except ValueError as exc:
                        return self._send(400, {"error": str(exc)})
                    if final and (
                        job.received_bytes + len(body) != job.expected_bytes
                        or job.received_records + len(records) != job.expected_records
                    ):
                        return self._send(400, {"error": "job size or record count mismatch"})
                    job.queue.extend(records)
                    job.queued_bytes += sum(len(r) for r in records)
                    job.received_chunks += 1
                    job.received_records += len(records)
                    job.received_bytes += len(body)
                    sim.cond.notify_all()
                    return self._send(200, {"received": index})

            def _create(self, body):
                try:
                    spec = json.loads(body)
                    job_id = str(spec["job_id"])
                    chunks = int(spec["chunks"])
                    total = int(spec.get("bytes", 0))
                    records = int(spec.get("records", 0))
                except (ValueError, KeyError, TypeError):
                    return self._send(400, {"error": "bad job"})
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id) or chunks < 1 or total < 0 or records < 0:
                    return self._send(400, {"error": "bad job"})
                with sim.cond:
                    existing = sim.jobs.get(job_id)
                    if existing is not None:
                        # Retried create: idempotent, never a second job.
                        return self._send(200, existing.status(sim.boot_id))
                    if sim.printer_state not in ("ready", "busy"):
                        return self._send(503, {"error": f"printer {sim.printer_state}"})
                    if sim.active is not None and sim.active.state not in FINAL:
                        return self._send(409, {"error": "busy", "active_job_id": sim.active.job_id})
                    if total > sim.max_job_bytes:
                        return self._send(413, {"error": "job too large"})
                    job = SimJob(job_id, spec.get("kind", "receipt"), chunks, records, total)
                    sim.jobs[job_id] = job
                    while len(sim.jobs) > 32:
                        sim.jobs.popitem(last=False)
                    sim.active = job
                    sim.cond.notify_all()
                    return self._send(201, job.status(sim.boot_id))

        return Handler

    # --- helpers for tests -------------------------------------------------------------

    def wait_for(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        with self.cond:
            while not predicate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.cond.wait(min(remaining, 0.05))
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default=os.environ.get("PC1_SERVER_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("PC1_RELAY_TOKEN", ""))
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--advertise-url", help="callback URL to report (default http://HOST:PORT)")
    parser.add_argument("--baud", type=int, default=9600, help="simulated UART speed; 0 = instant")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "testing" / "artifacts" / "relay"))
    parser.add_argument("--max-chunk-bytes", type=int, default=8192)
    args = parser.parse_args()
    if not args.token:
        parser.error("set PC1_RELAY_TOKEN or pass --token")

    logging.basicConfig(level=logging.WARNING)
    sim = RelaySimulator(
        args.server,
        args.token,
        host=args.host,
        port=args.port,
        baud=args.baud,
        out_dir=args.out,
        max_chunk_bytes=args.max_chunk_bytes,
        advertise_url=args.advertise_url,
    ).start()
    print(f"[SIM] listening on {sim.url}, reporting to {sim.server_url}, baud {args.baud or 'instant'}", flush=True)
    try:
        last_error = None
        while True:
            time.sleep(1)
            if sim.last_heartbeat_error != last_error:
                last_error = sim.last_heartbeat_error
                print(f"[SIM] heartbeat {'failing: ' + last_error if last_error else 'ok'}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()


if __name__ == "__main__":
    main()
