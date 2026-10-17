"""Production output: push print jobs to the ESP32-S3 relay over HTTP.

A receipt becomes a job of records (escpos.downloaded_receipt_records): the
preamble, one RAM bitmap definition + print per band and the cutter feed.
64-row bands avoid vertical stretching seen with GS v 0 at 9600 baud.
The job is created on the device,
streamed in chunks no larger than the device's limit, and then followed until
the device reports a final state (status callback, with polling as a fallback).
The printer reservation is held until then, so jobs never overlap.

Nothing is queued or replayed: if the device is offline, busy beyond a short
retry window, restarts mid-job or times out, the job fails and is logged.
See docs/relay-protocol.md.
"""

import logging
import threading
import time
import uuid
from typing import Optional

import requests

from app.drivers import escpos
from app.drivers.print_outputs import (
    PrintOutput,
    PrinterTransportError,
    PrinterUnavailableError,
)
from app.relay_auth import get_relay_token
from app.relay_link import FINAL_JOB_STATES, JobStatusReport, DeviceLink

logger = logging.getLogger(__name__)


class Esp32RelayOutput(PrintOutput):
    mode = "esp32"

    CONNECT_TIMEOUT = 3.05
    READ_TIMEOUT = 10.0
    BUSY_RETRY_SECONDS = 3.0
    POLL_INTERVAL_SECONDS = 5.0
    MAX_BACKOFF_SECONDS = 2.0
    BASE_JOB_TIMEOUT_SECONDS = 30.0
    CHUNK_RETRY_SECONDS = 15.0

    def __init__(self, link: Optional[DeviceLink] = None, session=None):
        if link is None:
            from app.relay_link import link as default_link

            link = default_link
        self.link = link
        self.session = session or requests.Session()
        # One job at a time, including tactile feeds.
        self._job_lock = threading.Lock()
        self.last_transport_stats: dict = {}

    # --- status --------------------------------------------------------------

    def is_available(self) -> bool:
        return self.link.availability()[0]

    def describe(self) -> dict:
        available, reason = self.link.availability()
        return {"mode": self.mode, "available": available, "detail": reason}

    # --- HTTP ----------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs):
        token = get_relay_token()
        base_url = self.link.base_url()
        if not token or not base_url:
            raise PrinterUnavailableError(self.link.availability()[1])
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        try:
            response = self.session.request(
                method,
                f"{base_url}{path}",
                headers=headers,
                timeout=(self.CONNECT_TIMEOUT, self.READ_TIMEOUT),
                **kwargs,
            )
        except requests.RequestException as exc:
            self.link.mark_unreachable(type(exc).__name__)
            raise PrinterUnavailableError(f"ESP32 relay unreachable: {exc}") from exc
        if response.status_code == 401:
            self.link.mark_unreachable("device rejected the relay token")
            raise PrinterUnavailableError("ESP32 relay rejected the token")
        return response

    @staticmethod
    def _retry_after(response) -> float:
        try:
            millis = int(response.headers.get("Retry-After-Ms", "200"))
        except ValueError:
            millis = 200
        return min(max(millis, 10) / 1000.0, Esp32RelayOutput.MAX_BACKOFF_SECONDS)

    # --- job push ------------------------------------------------------------

    def _limits(self):
        device = self.link.device
        if device is None:
            raise PrinterUnavailableError(self.link.availability()[1])
        return device

    def _check_job_active(self, job_id: str):
        job = self.link.job(job_id)
        if job is None or job["state"] in FINAL_JOB_STATES:
            raise PrinterTransportError(
                (job or {}).get("error") or "Relay job stopped before delivery finished"
            )

    def _create_job(self, job_id: str, kind: str, chunks: list, total_bytes: int):
        body = {
            "job_id": job_id,
            "kind": kind,
            "chunks": len(chunks),
            "records": sum(len(escpos.parse_framed_records(chunk)) for chunk in chunks),
            "bytes": total_bytes,
        }
        deadline = time.monotonic() + self.BUSY_RETRY_SECONDS
        while True:
            self._check_job_active(job_id)
            response = self._request("POST", "/jobs", json=body)
            if response.status_code in (200, 201):
                return
            if response.status_code == 409 and time.monotonic() < deadline:
                # Usually a tactile feed still finishing; wait briefly.
                time.sleep(0.1)
                continue
            if response.status_code == 409:
                raise PrinterUnavailableError("ESP32 relay is busy with another job")
            if response.status_code == 413:
                raise PrinterTransportError("Receipt is too large for the ESP32 relay")
            raise PrinterTransportError(
                f"ESP32 relay refused the job: HTTP {response.status_code}"
            )

    def _send_chunk(self, job_id: str, index: int, chunk: bytes, final: bool, cancel_event):
        deadline = time.monotonic() + self.CHUNK_RETRY_SECONDS
        while True:
            self._check_job_active(job_id)
            if cancel_event is not None and cancel_event.is_set():
                return False
            response = self._request(
                "PUT",
                f"/jobs/{job_id}/chunks/{index}",
                data=chunk,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-PC1-Final-Chunk": "1" if final else "0",
                },
            )
            if response.status_code == 200:
                return True
            if response.status_code == 429:
                # Device buffer full: back off and resend the same chunk.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PrinterTransportError(
                        f"ESP32 relay buffer did not drain for chunk {index}"
                    )
                time.sleep(min(self._retry_after(response), remaining))
                continue
            if response.status_code == 404:
                raise PrinterTransportError("ESP32 relay lost the job (restarted?)")
            raise PrinterTransportError(
                f"ESP32 relay rejected chunk {index}: HTTP {response.status_code}"
            )

    def _push(self, records: list, kind: str, cancel_event=None) -> tuple:
        device = self._limits()
        chunks = escpos.split_records_into_chunks(records, device.limits.max_chunk_bytes)
        total = sum(len(c) for c in chunks)
        if total > device.limits.max_job_bytes:
            raise PrinterTransportError(
                f"Receipt of {total} bytes exceeds the device job limit {device.limits.max_job_bytes}"
            )
        job_id = uuid.uuid4().hex
        self.link.register_job(job_id, kind, total)
        try:
            self._create_job(job_id, kind, chunks, total)
            for index, chunk in enumerate(chunks):
                if not self._send_chunk(job_id, index, chunk, index == len(chunks) - 1, cancel_event):
                    self._send_cancel(job_id)
                    break
        except Exception as exc:
            self.link.finish_job(job_id, "failed", str(exc))
            # A chunk may already be queued at the device; request cancellation
            # without ever replaying an uncertain delivery.
            self._send_cancel(job_id)
            raise
        return job_id, total

    def _send_cancel(self, job_id: str):
        try:
            self._request("POST", f"/jobs/{job_id}/cancel")
        except PrinterTransportError:
            logger.debug("Relay cancel request failed", exc_info=True)

    def _poll(self, job_id: str):
        try:
            response = self._request("GET", f"/jobs/{job_id}")
        except PrinterTransportError:
            return
        if response.status_code == 404:
            self.link.finish_job(job_id, "failed", "Device no longer knows the job")
            return
        if response.status_code == 200:
            try:
                self.link.update_job(job_id, JobStatusReport(**response.json()))
            except Exception:
                logger.debug("Unreadable relay job status", exc_info=True)

    def _await(self, job_id: str, total_bytes: int, cancel_event) -> dict:
        device = self.link.device
        baudrate = device.printer.baudrate if device else 9600
        # ~10 bits per byte on the UART, with generous slack for paper motion.
        deadline = time.monotonic() + self.BASE_JOB_TIMEOUT_SECONDS + 2.0 * total_bytes * 10 / baudrate
        cancel_sent = False
        last_poll = time.monotonic()
        while True:
            job = self.link.wait_job(job_id, timeout=0.25)
            if job is None:
                raise PrinterTransportError("Relay job record disappeared")
            if job["state"] in FINAL_JOB_STATES:
                stats = {
                    "job_id": job_id,
                    "state": job["state"],
                    "bytes_sent": job["bytes_written"],
                    "records_written": job["records_written"],
                    "cancelled": job["state"] == "cancelled",
                }
                self.last_transport_stats = stats
                if job["state"] == "failed":
                    raise PrinterTransportError(job["error"] or "ESP32 relay job failed")
                return stats
            now = time.monotonic()
            if cancel_event is not None and cancel_event.is_set() and not cancel_sent:
                self._send_cancel(job_id)
                cancel_sent = True
            if now - last_poll >= self.POLL_INTERVAL_SECONDS:
                last_poll = now
                self._poll(job_id)
            if now >= deadline:
                self.link.finish_job(job_id, "failed", "Timed out waiting for the device")
                self._send_cancel(job_id)

    # --- PrintOutput -----------------------------------------------------------

    def print_receipt(self, img, *, feed_lines_after, cancel_event):
        available, reason = self.link.availability()
        if not available:
            raise PrinterUnavailableError(f"ESP32 relay unavailable: {reason}")
        width_dots = self._limits().printer.width_dots
        if img.width > width_dots:
            raise PrinterTransportError(
                f"Receipt is {img.width} dots wide; the printer takes {width_dots}"
            )
        # Each record must fit a chunk. Older/smaller relays can use shorter
        # eight-row-aligned bands without falling back to the stretching
        # streaming raster command. Cap image data at the verified 3 KiB.
        width_bytes = (img.width + 7) // 8
        chunk_limit = self._limits().limits.max_chunk_bytes
        band_rows = min(
            escpos.DOWNLOADED_BAND_ROWS,
            min(3072, chunk_limit - 9) // width_bytes // 8 * 8,
        )  # 4-byte definition + 3-byte print + 2-byte record framing.
        if band_rows < 8:
            raise PrinterTransportError("ESP32 chunk limit cannot fit an eight-row bitmap band")
        records = escpos.downloaded_receipt_records(img, feed_lines_after, band_rows=band_rows)
        with self._job_lock:
            job_id, total = self._push(records, "receipt", cancel_event)
            stats = self._await(job_id, total, cancel_event)
        stats.update({"width": img.width, "height": img.height,
                      "strips": len(records) - 1 - (1 if feed_lines_after else 0)})
        logger.info("Relay receipt %s %s: %d bytes", job_id, stats["state"], total)
        return stats

    def _fire_and_forget(self, records: list, kind: str):
        """Small jobs (tactile feeds) are pushed but not followed to completion."""
        if not self.is_available():
            return
        if not self._job_lock.acquire(timeout=0.5):
            return
        try:
            self._push(records, kind)
        except PrinterTransportError:
            logger.debug("Relay %s job failed", kind, exc_info=True)
        finally:
            self._job_lock.release()

    def feed(self, dots: int) -> None:
        data = escpos.feed_dots(dots)
        if data:
            self._fire_and_forget([data], "feed")

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass
