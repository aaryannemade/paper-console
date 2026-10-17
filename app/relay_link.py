"""Container side of the ESP32 relay link (migration Step 3c).

The ESP32 posts a heartbeat every few seconds with its identity, boot id, the URL
the container should push print jobs to, printer state and limits. Link status
in the settings UI is derived from these heartbeats and from push results, not
from WiFi association. Job status callbacks from the device wake the print
output waiting for a job to finish. See docs/relay-protocol.md.
"""

import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Literal, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth import require_admin_access
from app.relay_auth import RelayTokenError, get_relay_token, require_relay_token

logger = logging.getLogger(__name__)

RELAY_URL_ENV = "PC1_RELAY_URL"
HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_TIMEOUT_SECONDS = 30
JOB_HISTORY_LIMIT = 32

PrinterState = Literal["ready", "busy", "paper_out", "error", "offline"]
JobState = Literal["receiving", "printing", "completed", "cancelled", "failed"]
FINAL_JOB_STATES = frozenset({"completed", "cancelled", "failed"})
PRINTABLE_STATES = frozenset({"ready", "busy"})


def _validate_http_url(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("must be an http(s) URL with a host")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("must not contain credentials, a query or a fragment")
    return value


class PrinterReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    state: PrinterState = "ready"
    detail: Optional[str] = Field(default=None, max_length=200)
    width_dots: int = Field(default=384, ge=8, le=2048)
    baudrate: int = Field(default=9600, ge=1200, le=921600)


class DeviceLimits(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_chunk_bytes: int = Field(default=8192, ge=256, le=1024 * 1024)
    max_job_bytes: int = Field(default=1024 * 1024, ge=1024, le=64 * 1024 * 1024)


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    device_id: str = Field(min_length=1, max_length=64)
    boot_id: str = Field(min_length=1, max_length=64)
    firmware: str = Field(default="", max_length=64)
    callback_url: Optional[str] = Field(default=None, max_length=256)
    printer: PrinterReport = Field(default_factory=PrinterReport)
    limits: DeviceLimits = Field(default_factory=DeviceLimits)
    active_job_id: Optional[str] = Field(default=None, max_length=64)

    @field_validator("callback_url")
    @classmethod
    def _check_url(cls, value):
        return _validate_http_url(value)


class JobStatusReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    state: JobState
    boot_id: Optional[str] = Field(default=None, max_length=64)
    records_written: int = Field(default=0, ge=0)
    bytes_written: int = Field(default=0, ge=0)
    error: Optional[str] = Field(default=None, max_length=300)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DeviceLink:
    """Thread-safe view of the relay device, its heartbeats and print jobs."""

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._cond = threading.Condition()
        self.reset()

    def reset(self):
        with self._cond:
            self.device: Optional[Heartbeat] = None
            self.last_heartbeat_at: Optional[float] = None
            self.last_heartbeat_wall: Optional[str] = None
            self.unreachable_error: Optional[str] = None
            self.unreachable_wall: Optional[str] = None
            self.jobs: "OrderedDict[str, dict]" = OrderedDict()
            self._cond.notify_all()

    # --- heartbeat -----------------------------------------------------------

    def record_heartbeat(self, heartbeat: Heartbeat) -> dict:
        with self._cond:
            previous = self.device
            if previous is not None and previous.boot_id != heartbeat.boot_id:
                logger.warning(
                    "Relay device %s restarted (boot %s -> %s)",
                    heartbeat.device_id,
                    previous.boot_id,
                    heartbeat.boot_id,
                )
                # Never replay: delivery of an interrupted job is uncertain.
                for job in self.jobs.values():
                    if job["state"] not in FINAL_JOB_STATES:
                        self._finish_locked(job, "failed", "Device restarted during the job")
            elif previous is None:
                logger.info(
                    "Relay device %s connected (firmware %s)",
                    heartbeat.device_id,
                    heartbeat.firmware or "unknown",
                )
            if self.unreachable_error:
                logger.info("Relay device %s reachable again", heartbeat.device_id)
            self.device = heartbeat
            self.last_heartbeat_at = self._clock()
            self.last_heartbeat_wall = _now_iso()
            self.unreachable_error = None
            self.unreachable_wall = None
            if heartbeat.active_job_id and heartbeat.active_job_id not in self.jobs:
                # E.g. a job pushed before the container restarted; let it finish.
                logger.info("Relay device reports unknown active job %s", heartbeat.active_job_id)
            self._cond.notify_all()
        return {
            "heartbeat_interval_s": HEARTBEAT_INTERVAL_SECONDS,
            "heartbeat_timeout_s": HEARTBEAT_TIMEOUT_SECONDS,
        }

    def mark_unreachable(self, error: str):
        with self._cond:
            if self.unreachable_error is None:
                logger.warning("Relay device unreachable: %s", error)
            self.unreachable_error = error
            self.unreachable_wall = _now_iso()

    def base_url(self) -> Optional[str]:
        pinned = os.environ.get(RELAY_URL_ENV, "").strip()
        if pinned:
            return _validate_http_url(pinned)
        with self._cond:
            return self.device.callback_url if self.device else None

    def availability(self) -> tuple:
        """(available, reason)."""
        try:
            if get_relay_token() is None:
                return False, "PC1_RELAY_TOKEN is not set"
        except RelayTokenError as exc:
            return False, str(exc)
        try:
            base_url = self.base_url()
        except ValueError:
            return False, f"{RELAY_URL_ENV} is not a valid http(s) URL"
        with self._cond:
            if self.device is None:
                return False, "No heartbeat from the device yet"
            age = self._clock() - self.last_heartbeat_at
            if age > HEARTBEAT_TIMEOUT_SECONDS:
                return False, f"No heartbeat for {int(age)} s"
            if self.unreachable_error:
                return False, f"Device unreachable: {self.unreachable_error}"
            if not base_url:
                return False, "Device did not report a callback URL"
            state = self.device.printer.state
            if state not in PRINTABLE_STATES:
                detail = self.device.printer.detail
                return False, f"Printer {state.replace('_', ' ')}" + (f": {detail}" if detail else "")
        return True, "online"

    # --- jobs ----------------------------------------------------------------

    def register_job(self, job_id: str, kind: str, total_bytes: int):
        with self._cond:
            self.jobs[job_id] = {
                "job_id": job_id,
                "kind": kind,
                "state": "receiving",
                "bytes": total_bytes,
                "bytes_written": 0,
                "records_written": 0,
                "error": None,
                "created": _now_iso(),
                "updated": _now_iso(),
                "boot_id": self.device.boot_id if self.device else None,
            }
            while len(self.jobs) > JOB_HISTORY_LIMIT:
                self.jobs.popitem(last=False)

    def update_job(self, job_id: str, report: JobStatusReport) -> bool:
        with self._cond:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            if job["state"] in FINAL_JOB_STATES:
                return True  # late/duplicate report; the outcome is fixed
            if report.boot_id and report.boot_id != job["boot_id"]:
                # Reports from an earlier boot must never complete a new job.
                return True
            job["bytes_written"] = report.bytes_written
            job["records_written"] = report.records_written
            if report.state in FINAL_JOB_STATES:
                self._finish_locked(job, report.state, report.error)
            else:
                job["state"] = report.state
                job["updated"] = _now_iso()
            self._cond.notify_all()
            return True

    def finish_job(self, job_id: str, state: str, error: Optional[str] = None):
        with self._cond:
            job = self.jobs.get(job_id)
            if job is not None and job["state"] not in FINAL_JOB_STATES:
                self._finish_locked(job, state, error)

    def _finish_locked(self, job: dict, state: str, error: Optional[str]):
        job["state"] = state
        job["error"] = error
        job["updated"] = _now_iso()
        log = logger.warning if state == "failed" else logger.info
        log("Relay job %s (%s) %s%s", job["job_id"], job["kind"], state, f": {error}" if error else "")
        self._cond.notify_all()

    def job(self, job_id: str) -> Optional[dict]:
        with self._cond:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    def wait_job(self, job_id: str, timeout: float) -> Optional[dict]:
        """Wait up to `timeout` for a final state; return the job snapshot."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                job = self.jobs.get(job_id)
                if job is None or job["state"] in FINAL_JOB_STATES:
                    return dict(job) if job else None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return dict(job)
                self._cond.wait(remaining)

    # --- reporting -----------------------------------------------------------

    def snapshot(self) -> dict:
        available, reason = self.availability()
        with self._cond:
            device = self.device
            age = (
                round(self._clock() - self.last_heartbeat_at, 1)
                if self.last_heartbeat_at is not None
                else None
            )
            receipts = [j for j in self.jobs.values() if j["kind"] == "receipt"]
            last_job = dict(receipts[-1]) if receipts else None
            if device is None:
                state = "never_seen"
            elif age is not None and age > HEARTBEAT_TIMEOUT_SECONDS:
                state = "offline"
            elif self.unreachable_error:
                state = "unreachable"
            else:
                state = "online"
            return {
                "state": state,
                "available": available,
                "reason": reason,
                "device_id": device.device_id if device else None,
                "firmware": device.firmware if device else None,
                "boot_id": device.boot_id if device else None,
                "callback_url": device.callback_url if device else None,
                "pinned_url": os.environ.get(RELAY_URL_ENV, "").strip() or None,
                "printer_state": device.printer.state if device else None,
                "printer_detail": device.printer.detail if device else None,
                "last_heartbeat": self.last_heartbeat_wall,
                "heartbeat_age_s": age,
                "unreachable_error": self.unreachable_error,
                "last_job": last_job,
            }


LATE_HEARTBEAT_SECONDS = 2 * HEARTBEAT_INTERVAL_SECONDS
ACTIVE_JOB_STATES = frozenset({"receiving", "printing"})


def status_indicator(snapshot: dict, output_mode: str, output_available: bool) -> dict:
    """Coarse green/amber/red summary for the settings top bar.

    Green means the relay can accept a print. It does not prove paper is loaded:
    the device does not sense the printer's physical state.
    """
    if output_mode == "console":
        return {"level": "amber", "label": "Console output: no printer connected"}
    if output_mode == "usb":
        if output_available:
            return {"level": "green", "label": "USB printer ready"}
        return {"level": "red", "label": "USB printer unavailable"}
    if output_mode != "esp32":
        return {"level": "red", "label": f"Unknown print output: {output_mode}"}

    if not snapshot.get("available"):
        state = snapshot.get("state")
        label = {
            "never_seen": "Waiting for ESP32",
            "offline": "ESP32 offline",
            "unreachable": "ESP32 unreachable",
        }.get(state, "Printer not ready")
        return {"level": "red", "label": label}

    last_job = snapshot.get("last_job") or {}
    if snapshot.get("printer_state") == "busy" or last_job.get("state") in ACTIVE_JOB_STATES:
        return {"level": "amber", "label": "Printing"}
    age = snapshot.get("heartbeat_age_s")
    if age is not None and age > LATE_HEARTBEAT_SECONDS:
        return {"level": "amber", "label": f"ESP32 heartbeat late ({int(age)} s)"}
    return {"level": "green", "label": "ESP32 online, ready to print"}


link = DeviceLink()

router = APIRouter(tags=["device"])


@router.post("/api/device/heartbeat", dependencies=[Depends(require_relay_token)])
def post_heartbeat(heartbeat: Heartbeat):
    """Device identity, callback URL, printer state and limits; send every interval."""
    return link.record_heartbeat(heartbeat)


@router.post(
    "/api/device/jobs/{job_id}/status", dependencies=[Depends(require_relay_token)]
)
def post_job_status(job_id: str, report: JobStatusReport):
    """Progress/final state of a print job pushed by the container."""
    if not link.update_job(job_id, report):
        raise HTTPException(status_code=404, detail="Unknown job")
    return {"job_id": job_id, "state": (link.job(job_id) or {}).get("state")}


@router.get("/api/printer/link", dependencies=[Depends(require_admin_access)])
def get_printer_link():
    """Relay link status for the settings UI."""
    import app.hardware as hardware

    snapshot = link.snapshot()
    output_mode = getattr(hardware.printer, "output_mode", "unknown")
    snapshot["output_mode"] = output_mode
    snapshot["indicator"] = status_indicator(
        snapshot, output_mode, hardware.printer_is_available()
    )
    return snapshot
