"""ESP32 relay print transport against the simulator (migration Step 3c).

Both directions use real HTTP: the container's relay endpoints run in a Uvicorn
thread, the simulator runs its own HTTP server and posts heartbeats/status back.
"""

import asyncio
import random
import socket
import threading
import time

import pytest
import requests
import uvicorn
from fastapi import FastAPI
from PIL import Image, ImageChops

import app.device_input as device_input
import app.hardware as hardware
import app.main as main
import app.relay_link as relay_link
import app.selection_mode as selection_mode
from app.drivers import escpos
from app.drivers.esp32_relay_output import Esp32RelayOutput
from app.drivers.print_outputs import PrinterTransportError, PrinterUnavailableError
from app.drivers.printer_raster import RasterPrinter
from app.relay_link import DeviceLink, Heartbeat, JobStatusReport
from testing.relay_simulator import RelaySimulator

TOKEN = "relay-test-token-0123456789"


def _image(height=100, seed=1):
    rng = random.Random(seed)
    img = Image.new("1", (384, height), 1)
    pixels = img.load()
    for _ in range(4000):
        pixels[rng.randrange(384), rng.randrange(height)] = 0
    return img


def _same(a, b):
    return a.size == b.size and ImageChops.difference(a.convert("L"), b.convert("L")).getbbox() is None


# --- framing ------------------------------------------------------------------


def test_records_frame_split_and_parse_round_trip():
    records = escpos.receipt_records(_image(50), feed_lines_after=3)
    assert records[0] == escpos.JOB_PREAMBLE and records[-1] == escpos.feed_lines(3)
    assert len(records) == 1 + 3 + 1  # preamble, 3 strips, feed

    chunks = escpos.split_records_into_chunks(records, 2048)
    assert len(chunks) > 1 and all(len(c) <= 2048 for c in chunks)
    parsed = [r for c in chunks for r in escpos.parse_framed_records(c)]
    assert parsed == records

    with pytest.raises(ValueError):
        escpos.split_records_into_chunks(records, 512)  # a strip does not fit
    for bad in (b"\x00", b"\x00\x05ab", b"\x00\x00"):
        with pytest.raises(ValueError):
            escpos.parse_framed_records(bad)


def test_decode_raster_inverts_encoding():
    img = _image(61)
    data = b"".join(escpos.receipt_records(img, 2))
    assert _same(escpos.decode_raster(data), img)
    assert escpos.decode_raster(escpos.feed_lines(2)) is None


# --- link state ---------------------------------------------------------------------


def _heartbeat(**overrides):
    payload = {
        "device_id": "dev1",
        "boot_id": "boot1",
        "callback_url": "http://192.168.1.50:80/",
    }
    payload.update(overrides)
    return Heartbeat(**payload)


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("PC1_RELAY_TOKEN", TOKEN)
    monkeypatch.delenv("PC1_RELAY_TOKEN_FILE", raising=False)
    monkeypatch.delenv(relay_link.RELAY_URL_ENV, raising=False)


def test_link_availability_follows_heartbeats(token_env):
    now = [100.0]
    link = DeviceLink(clock=lambda: now[0])
    assert link.availability() == (False, "No heartbeat from the device yet")

    reply = link.record_heartbeat(_heartbeat())
    assert reply["heartbeat_interval_s"] == relay_link.HEARTBEAT_INTERVAL_SECONDS
    assert link.availability() == (True, "online")
    assert link.base_url() == "http://192.168.1.50:80"

    now[0] += relay_link.HEARTBEAT_TIMEOUT_SECONDS + 1
    assert link.availability()[0] is False
    assert link.snapshot()["state"] == "offline"

    link.record_heartbeat(_heartbeat(printer={"state": "paper_out", "detail": "lid open"}))
    assert link.availability() == (False, "Printer paper out: lid open")

    link.record_heartbeat(_heartbeat())
    link.mark_unreachable("ConnectionError")
    assert link.snapshot()["state"] == "unreachable"
    link.record_heartbeat(_heartbeat())
    assert link.availability()[0] is True


def test_status_indicator_levels(token_env):
    now = [100.0]
    link = DeviceLink(clock=lambda: now[0])

    def level(mode="esp32", available=True):
        return relay_link.status_indicator(link.snapshot(), mode, available)["level"]

    assert level() == "red"  # never seen
    link.record_heartbeat(_heartbeat())
    assert level() == "green"

    now[0] += relay_link.LATE_HEARTBEAT_SECONDS + 1
    assert level() == "amber"  # late but not yet timed out
    now[0] += relay_link.HEARTBEAT_TIMEOUT_SECONDS
    assert level() == "red"  # offline

    link.record_heartbeat(_heartbeat(printer={"state": "busy"}))
    assert level() == "amber"
    link.record_heartbeat(_heartbeat())
    link.register_job("j1", "receipt", 100)
    assert level() == "amber"  # pushing a receipt
    link.finish_job("j1", "completed")
    assert level() == "green"

    link.record_heartbeat(_heartbeat(printer={"state": "error"}))
    assert level() == "red"
    link.record_heartbeat(_heartbeat())
    link.mark_unreachable("ConnectionError")
    assert level() == "red"

    assert level("console") == "amber"
    assert level("usb", True) == "green"
    assert level("usb", False) == "red"


def test_printer_link_endpoint_includes_indicator(token_env):
    assert hardware.printer.output_mode == "console"  # test default output
    data = relay_link.get_printer_link()
    assert data["output_mode"] == "console"
    assert data["indicator"]["level"] == "amber"


def test_link_requires_token(monkeypatch):
    monkeypatch.delenv("PC1_RELAY_TOKEN", raising=False)
    monkeypatch.delenv("PC1_RELAY_TOKEN_FILE", raising=False)
    link = DeviceLink()
    link.record_heartbeat(_heartbeat())
    assert link.availability() == (False, "PC1_RELAY_TOKEN is not set")


def test_pinned_relay_url_overrides_reported_callback(token_env, monkeypatch):
    link = DeviceLink()
    link.record_heartbeat(_heartbeat())
    monkeypatch.setenv(relay_link.RELAY_URL_ENV, "http://printer.lan:8080")
    assert link.base_url() == "http://printer.lan:8080"


@pytest.mark.parametrize("url", ["ftp://x", "http://", "http://u:p@h/", "http://h/?q=1", "nope"])
def test_heartbeat_rejects_bad_callback_url(url):
    with pytest.raises(ValueError):
        _heartbeat(callback_url=url)


def test_device_restart_fails_open_jobs_without_replay(token_env):
    link = DeviceLink()
    link.record_heartbeat(_heartbeat())
    link.register_job("j1", "receipt", 100)
    link.update_job("j1", JobStatusReport(state="printing", bytes_written=10))
    link.record_heartbeat(_heartbeat(boot_id="boot2"))
    job = link.job("j1")
    assert job["state"] == "failed" and "restarted" in job["error"]
    # A late report from the old boot cannot resurrect it.
    link.update_job("j1", JobStatusReport(state="completed"))
    assert link.job("j1")["state"] == "failed"


def test_status_from_wrong_boot_cannot_complete_new_job(token_env):
    link = DeviceLink()
    link.record_heartbeat(_heartbeat(boot_id="new-boot"))
    link.register_job("new-job", "receipt", 100)
    assert link.update_job(
        "new-job", JobStatusReport(state="completed", boot_id="old-boot")
    )
    assert link.job("new-job")["state"] == "receiving"


def test_chunk_retries_stop_when_buffer_stays_full(token_env, monkeypatch):
    link = DeviceLink()
    link.record_heartbeat(_heartbeat())
    output = Esp32RelayOutput(link=link)
    link.register_job("stuck", "receipt", 100)

    class Full:
        status_code = 429
        headers = {"Retry-After-Ms": "10"}

    monkeypatch.setattr(output, "_request", lambda *a, **kw: Full())
    monkeypatch.setattr(output, "CHUNK_RETRY_SECONDS", 0.03)
    with pytest.raises(PrinterTransportError, match="did not drain"):
        output._send_chunk("stuck", 0, b"record", True, threading.Event())


def test_reboot_stops_chunk_transfer_immediately(token_env, monkeypatch):
    link = DeviceLink()
    link.record_heartbeat(_heartbeat())
    output = Esp32RelayOutput(link=link)
    link.register_job("rebooted", "receipt", 100)
    link.record_heartbeat(_heartbeat(boot_id="new-boot"))
    monkeypatch.setattr(output, "_request", lambda *a, **kw: pytest.fail("sent after reboot"))
    with pytest.raises(PrinterTransportError, match="restarted"):
        output._send_chunk("rebooted", 0, b"record", True, threading.Event())


def test_device_routes_are_registered_with_correct_auth():
    import app.relay_auth as relay_auth

    routes = {getattr(r, "path", ""): r for r in main.app.routes}
    for path in ("/api/device/heartbeat", "/api/device/jobs/{job_id}/status"):
        deps = [d.call for d in routes[path].dependant.dependencies]
        assert relay_auth.require_relay_token in deps
    deps = [d.call for d in routes["/api/printer/link"].dependant.dependencies]
    assert main.require_admin_access in deps


# --- end to end against the simulator ---------------------------------------------


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def container(token_env):
    relay_link.link.reset()
    api = FastAPI()
    api.include_router(relay_link.router)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
    relay_link.link.reset()


@pytest.fixture
def make_sim(container):
    sims = []

    def factory(**kwargs):
        kwargs.setdefault("baud", 0)
        kwargs.setdefault("heartbeat_interval", 0.3)
        sim = RelaySimulator(container, kwargs.pop("token", TOKEN), **kwargs).start()
        sims.append(sim)
        deadline = time.monotonic() + 5
        while not relay_link.link.availability()[0] and time.monotonic() < deadline:
            time.sleep(0.02)
        return sim

    yield factory
    for sim in sims:
        sim.stop()


@pytest.fixture
def output(container):
    out = Esp32RelayOutput(link=relay_link.link)
    yield out
    out.close()


def test_receipt_round_trip_is_byte_exact(make_sim, output):
    sim = make_sim()
    img = _image(130)

    stats = output.print_receipt(img, feed_lines_after=4, cancel_event=threading.Event())

    assert stats["state"] == "completed" and stats["cancelled"] is False
    assert stats["strips"] == 3
    records = escpos.downloaded_receipt_records(img, 4)
    assert stats["bytes_sent"] == sum(len(r) for r in records)
    job = sim.jobs[stats["job_id"]]
    assert bytes(job.data) == b"".join(records)
    (job_id, upright), = sim.receipts
    decoded = upright.rotate(180)
    assert _same(decoded.crop((0, 0, img.width, img.height)), img)
    assert decoded.height == 136  # Final band padded to an eight-row boundary.
    assert relay_link.link.snapshot()["last_job"]["state"] == "completed"


def test_device_refetches_channel_names_only_when_hash_changes(make_sim, monkeypatch):
    import app.config as config

    settings = config.Settings()
    monkeypatch.setattr(config, "settings", settings)
    sim = make_sim()
    assert sim.wait_for(lambda: sim.channel_fetches == 1)
    first_hash = sim.channels_hash
    sim.send_heartbeat()
    sim.send_heartbeat()
    assert sim.channel_fetches == 1  # unchanged names: no refetch

    settings.channels[2].name = "Work update"
    sim.send_heartbeat()
    assert sim.channel_fetches == 2
    assert sim.channel_names[2] == "Work update" and sim.channels_hash != first_hash


def test_raster_printer_renders_through_relay(make_sim, output):
    sim = make_sim()
    printer = RasterPrinter(output=output)
    printer.print_header("Relay")
    printer.print_body("Rendered in the container, printed by the ESP32.")
    printer.flush_buffer()
    assert len(sim.receipts) == 1
    assert sim.receipts[0][1].width == 384


def test_small_chunks_and_full_buffer_still_deliver_everything(make_sim, output):
    sim = make_sim(max_chunk_bytes=1200, buffer_limit_bytes=2400, baud=200_000)
    img = _image(240)
    stats = output.print_receipt(img, feed_lines_after=1, cancel_event=threading.Event())
    job = sim.jobs[stats["job_id"]]
    assert job.received_chunks > 5
    assert _same(escpos.decode_raster(bytes(job.data)), img)


def test_cancel_stops_between_records(make_sim, output):
    sim = make_sim(baud=9600)  # ~3.2 s per 64-row band
    cancel = threading.Event()
    result = {}

    def run():
        result["stats"] = output.print_receipt(_image(240), feed_lines_after=4, cancel_event=cancel)

    worker = threading.Thread(target=run)
    worker.start()
    assert sim.wait_for(lambda: sim.active is not None and sim.active.records_written >= 1)
    cancel.set()
    worker.join(timeout=10)

    stats = result["stats"]
    assert stats["cancelled"] is True and stats["state"] == "cancelled"
    job = sim.jobs[stats["job_id"]]
    total = len(escpos.downloaded_receipt_records(_image(240), 4))
    assert 1 <= job.records_written < total
    # Whole records only: the written bytes parse as complete strips.
    decoded = escpos.decode_raster(bytes(job.data))
    if decoded is not None:  # Cancellation may win before the first bitmap band.
        assert decoded.height % 64 == 0


def test_lost_status_callbacks_fall_back_to_polling(make_sim, output, monkeypatch):
    monkeypatch.setattr(Esp32RelayOutput, "POLL_INTERVAL_SECONDS", 0.2)
    make_sim(report_status=False)
    stats = output.print_receipt(_image(48), feed_lines_after=0, cancel_event=threading.Event())
    assert stats["state"] == "completed"


def test_device_restart_mid_job_fails_and_is_not_replayed(make_sim, output):
    sim = make_sim(baud=9600)
    errors = []

    def run():
        try:
            output.print_receipt(_image(240), feed_lines_after=0, cancel_event=threading.Event())
        except PrinterTransportError as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=run)
    worker.start()
    assert sim.wait_for(lambda: sim.active is not None and sim.active.records_written >= 1)
    sim.reboot()
    worker.join(timeout=10)

    assert errors and "restarted" in errors[0]
    time.sleep(0.5)
    assert sim.jobs == {}  # nothing re-sent after the reboot


def test_unreachable_device_is_reported_and_recovers(make_sim, output):
    sim = make_sim()
    port = sim.port
    sim.stop()

    with pytest.raises(PrinterUnavailableError):
        output.print_receipt(_image(24), feed_lines_after=0, cancel_event=threading.Event())
    assert output.is_available() is False
    assert relay_link.link.snapshot()["state"] == "unreachable"

    make_sim(port=port)
    assert output.is_available() is True


def test_busy_device_rejects_after_short_retry(make_sim, output, monkeypatch):
    monkeypatch.setattr(Esp32RelayOutput, "BUSY_RETRY_SECONDS", 0.3)
    sim = make_sim(baud=1200)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    requests.post(f"{sim.url}/jobs", json={"job_id": "other", "chunks": 1}, headers=headers, timeout=5)

    with pytest.raises(PrinterUnavailableError, match="busy"):
        output.print_receipt(_image(24), feed_lines_after=0, cancel_event=threading.Event())


def test_token_mismatch_marks_device_unreachable(make_sim, output):
    sim = make_sim()
    sim.token = "a-different-token-0123456789"  # device configured with another token

    with pytest.raises(PrinterUnavailableError, match="token"):
        output.print_receipt(_image(24), feed_lines_after=0, cancel_event=threading.Event())
    assert relay_link.link.snapshot()["unreachable_error"] == "device rejected the relay token"


def test_tactile_feed_is_a_small_unfollowed_job(make_sim, output):
    sim = make_sim()
    output.feed(12)
    assert sim.wait_for(lambda: any(j.kind == "feed" and j.state == "completed" for j in sim.jobs.values()))
    feed_job = next(j for j in sim.jobs.values() if j.kind == "feed")
    assert bytes(feed_job.data) == escpos.feed_dots(12)
    assert sim.receipts == []


def test_simulator_contract_is_idempotent(make_sim):
    sim = make_sim()
    h = {"Authorization": f"Bearer {TOKEN}"}
    chunk = escpos.frame_records([escpos.feed_dots(6)])
    body = {"job_id": "dup", "chunks": 1, "kind": "feed", "records": 1, "bytes": len(chunk)}
    assert requests.post(f"{sim.url}/jobs", json=body, headers=h, timeout=5).status_code == 201
    assert requests.post(f"{sim.url}/jobs", json=body, headers=h, timeout=5).status_code == 200
    url = f"{sim.url}/jobs/dup/chunks/0"
    final_headers = {**h, "X-PC1-Final-Chunk": "1"}
    assert requests.put(url, data=chunk, headers=final_headers, timeout=5).status_code == 200
    assert requests.put(url, data=chunk, headers=final_headers, timeout=5).json()["duplicate"] is True
    assert sim.wait_for(lambda: sim.jobs["dup"].state == "completed")
    assert bytes(sim.jobs["dup"].data) == escpos.feed_dots(6)
    assert requests.get(f"{sim.url}/jobs/dup", headers={}, timeout=5).status_code == 401


def test_simulator_rejects_incomplete_final_chunk(make_sim):
    sim = make_sim()
    h = {"Authorization": f"Bearer {TOKEN}"}
    chunk = escpos.frame_records([escpos.feed_dots(6)])
    requests.post(
        f"{sim.url}/jobs",
        json={"job_id": "short", "chunks": 1, "kind": "feed", "records": 2, "bytes": len(chunk)},
        headers=h,
        timeout=5,
    )
    url = f"{sim.url}/jobs/short/chunks/0"
    assert requests.put(url, data=chunk, headers=h, timeout=5).status_code == 400
    assert requests.put(
        url, data=chunk, headers={**h, "X-PC1-Final-Chunk": "1"}, timeout=5
    ).status_code == 400
    assert sim.jobs["short"].received_chunks == 0


# --- application flows: scheduled and input-triggered prints ------------------------


@pytest.fixture
def relay_printer(make_sim, output, monkeypatch):
    sim = make_sim()
    printer = RasterPrinter(output=output)
    monkeypatch.setattr(hardware, "printer", printer)
    monkeypatch.setattr(main, "printer", printer)
    monkeypatch.setattr(hardware, "print_in_progress", False)
    monkeypatch.setattr(hardware, "hold_action_in_progress", False)
    monkeypatch.setattr(hardware, "last_print_time", 0.0)
    selection_mode.exit_selection_mode()
    yield sim
    selection_mode.exit_selection_mode()
    hardware.clear_print_reservation()


def _text_channel(monkeypatch, position):
    from app.config import ChannelConfig, ChannelModuleAssignment, ModuleInstance, Settings

    settings = main.settings.model_copy(deep=True) if hasattr(main.settings, "model_copy") else Settings()
    settings.modules["relay-text"] = ModuleInstance(
        id="relay-text", type="text", name="Relay Note", config={"content": "Hello from the relay"}
    )
    settings.channels[position] = ChannelConfig(
        modules=[ChannelModuleAssignment(module_id="relay-text", order=0)]
    )
    monkeypatch.setattr(main, "settings", settings)


def test_scheduled_print_is_pushed_to_the_device(relay_printer, monkeypatch):
    _text_channel(monkeypatch, 3)
    assert hardware.try_begin_print_job() is True  # as the scheduler does
    asyncio.run(main.trigger_channel(3, scheduled=True))
    assert len(relay_printer.receipts) == 1
    assert hardware.print_in_progress is False


def test_button_press_event_prints_through_the_device(relay_printer, monkeypatch):
    _text_channel(monkeypatch, 6)
    proc = device_input.InputProcessor()
    proc.configure(main.dial, {
        "press": main.handle_button_press, "hold": main.handle_button_hold,
        "hold_release": main.handle_button_hold_release,
    })
    monkeypatch.setattr(main.dial, "current_position", 1)

    async def scenario():
        monkeypatch.setattr(main, "global_loop", asyncio.get_running_loop())
        event = device_input.DeviceInputEvent(
            event_id="e1", type="button", gesture="press", press_id=1, position=6
        )
        result = await asyncio.to_thread(proc.process, event)
        for _ in range(200):
            if relay_printer.receipts and not hardware.print_in_progress:
                break
            await asyncio.sleep(0.05)
        return result

    assert asyncio.run(scenario())["result"] == "printing_channel"
    assert len(relay_printer.receipts) == 1


def test_offline_device_skips_scheduled_print(relay_printer, monkeypatch):
    relay_printer.stop()
    relay_link.link.mark_unreachable("ConnectionError")
    assert hardware.try_begin_print_job() is False
