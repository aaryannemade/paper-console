import types
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

import app.config as config
import app.hardware as hardware_module
import app.slack_manager as slack_manager
from app.config import ModuleInstance, SlackConfig
from app.modules.slack import format_slack_receipt
from app.slack_manager import (
    SlackConnection,
    extract_images,
    extract_urls,
    get_slack_user_name,
    handle_channel_command,
    handle_channels_command,
    handle_message_event,
    slack_to_tiptap,
    transform_text,
)


@pytest.fixture(autouse=True)
def reset_manager_state():
    slack_manager._connections.clear()
    slack_manager._statuses.clear()
    yield
    slack_manager._connections.clear()
    slack_manager._statuses.clear()


@pytest.fixture
def mock_printer(monkeypatch):
    printer = MagicMock()
    monkeypatch.setattr(hardware_module, "printer", printer)
    return printer


@pytest.fixture
def mock_slack_client():
    client = AsyncMock()
    client.token = "xoxb-fake-token"
    return client


def _make_connection(**cfg_overrides) -> SlackConnection:
    cfg = SlackConfig(
        bot_token="xoxb-test",
        app_token="xapp-test",
        **cfg_overrides,
    )
    return SlackConnection("slack-mod-1", "TEAM SLACK", cfg)


def _message_body(**event_overrides):
    event = {
        "user": "U123",
        "channel": "D123",
        "channel_type": "im",
        "ts": "1234.5678",
        "blocks": [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "Check this link: "},
                            {"type": "link", "url": "https://example.com"},
                        ],
                    }
                ],
            }
        ],
    }
    event.update(event_overrides)
    return {"event": event}


# --- BLOCK CONVERSION UNIT TESTS (ported from PR #7) ---


def test_extract_urls():
    """URLs are extracted, deduped, and pulled from nested block structures."""
    blocks = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "text", "text": "Visit "},
                        {"type": "link", "url": "https://google.com"},
                        {"type": "text", "text": " and "},
                        {"type": "link", "url": "https://github.com"},
                        {"type": "text", "text": " and "},
                        {"type": "link", "url": "https://github.com"},
                    ],
                }
            ],
        },
        {"type": "divider"},  # Edge case: block without elements
    ]
    assert extract_urls(blocks) == ["https://google.com", "https://github.com"]
    assert extract_urls([]) == []


def test_transform_text_styles():
    slack_text = {
        "type": "text",
        "text": "Hello World",
        "style": {"bold": True, "italic": True},
    }
    result = transform_text(slack_text)
    assert result["text"] == "Hello World"
    assert {"type": "bold"} in result["marks"]
    assert {"type": "italic"} in result["marks"]


def test_transform_text_with_links():
    """Links become indexed references [N] matching the printed QR codes."""
    urls = ["https://google.com", "https://slack.com"]

    linked = {"type": "link", "url": "https://slack.com", "text": "Slack"}
    assert transform_text(linked, urls)["text"] == "Slack[2]"

    bare = {"type": "link", "url": "https://google.com"}
    assert transform_text(bare, urls)["text"] == "[1]"


def test_slack_to_tiptap_conversion():
    blocks = [
        {"type": "header", "text": {"text": "My Heading"}},
        {"type": "divider"},
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [{"type": "text", "text": "Paragraph content"}],
                }
            ],
        },
    ]
    doc = slack_to_tiptap(blocks)
    assert doc["type"] == "doc"
    assert doc["content"][0]["type"] == "heading"
    assert doc["content"][1]["type"] == "horizontalRule"
    assert doc["content"][2]["type"] == "paragraph"


def test_slack_to_tiptap_lists_and_quotes():
    blocks = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_list",
                    "style": "bullet",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [{"type": "text", "text": "item one"}],
                        }
                    ],
                },
                {
                    "type": "rich_text_quote",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [{"type": "text", "text": "quoted"}],
                        }
                    ],
                },
            ],
        }
    ]
    doc = slack_to_tiptap(blocks)
    assert doc["content"][0]["type"] == "bulletList"
    assert doc["content"][1]["type"] == "paragraph"
    assert doc["content"][1]["content"][0]["text"] == "quoted"


# --- ASYNC HELPERS ---


@pytest.mark.asyncio
async def test_get_slack_user_name_success(mock_slack_client):
    mock_slack_client.users_info.return_value.data = {"user": {"real_name": "Bob"}}
    assert await get_slack_user_name(mock_slack_client, "U123") == "Bob"


@pytest.mark.asyncio
async def test_get_slack_user_name_failure(mock_slack_client):
    mock_slack_client.users_info.side_effect = Exception("API Error")
    assert await get_slack_user_name(mock_slack_client, "U123") == "UNKNOWN"


@pytest.mark.asyncio
@patch("aiohttp.ClientSession.get")
async def test_extract_images_edge_cases(mock_get):
    """Image extraction skips non-images and failed downloads."""
    event = {
        "files": [
            {"mimetype": "image/jpeg", "url_private_download": "http://fake.com/img.jpg"},
            {"mimetype": "application/pdf", "url_private_download": "http://fake.com/doc.pdf"},
        ]
    }

    img = Image.new("RGB", (100, 100))
    img_byte_arr = BytesIO()
    img.save(img_byte_arr, format="JPEG")

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read = AsyncMock(return_value=img_byte_arr.getvalue())
    mock_response.__aenter__.return_value = mock_response
    mock_get.return_value = mock_response

    images = await extract_images(event, "token")
    assert len(images) == 1  # PDF is ignored
    assert isinstance(images[0], Image.Image)
    assert images[0].mode == "1"  # Prepared for thermal printing

    mock_response.status = 404
    assert await extract_images(event, "token") == []


# --- MESSAGE HANDLING FLOW ---


@pytest.mark.asyncio
async def test_print_message_flow(monkeypatch, mock_printer, mock_slack_client):
    """Happy path: reserve printer, print, swap reactions eyes -> printer."""
    reservations = []
    monkeypatch.setattr(
        hardware_module, "try_begin_print_job", lambda debounce=False: reservations.append("begin") or True
    )
    monkeypatch.setattr(
        hardware_module,
        "clear_print_reservation",
        lambda clear_hold=True: reservations.append("clear"),
    )
    monkeypatch.setattr(
        slack_manager, "get_slack_user_name", AsyncMock(return_value="TestUser")
    )
    dummy_img = Image.new("1", (384, 100))
    monkeypatch.setattr(slack_manager, "extract_images", AsyncMock(return_value=[dummy_img]))

    connection = _make_connection()
    ack = AsyncMock()

    await handle_message_event(connection, _message_body(), mock_slack_client, ack)

    ack.assert_awaited_once()
    assert reservations == ["begin", "clear"]

    # eyes added then removed, printer added
    added = [call.kwargs["name"] for call in mock_slack_client.reactions_add.call_args_list]
    removed = [call.kwargs["name"] for call in mock_slack_client.reactions_remove.call_args_list]
    assert added == ["eyes", "printer"]
    assert removed == ["eyes"]

    # Receipt content: header, sender caption, QR for the link, image
    mock_printer.print_header.assert_called_once_with("TEAM SLACK", icon="slack-logo")
    captions = [call.args[0] for call in mock_printer.print_caption.call_args_list]
    assert "From: TestUser" in captions
    assert mock_printer.print_qr.call_count == 1
    assert mock_printer.print_qr.call_args.kwargs["data"] == "https://example.com"
    mock_printer.print_image.assert_called_once_with(dummy_img)


@pytest.mark.asyncio
async def test_print_message_busy_printer_gives_feedback(monkeypatch, mock_printer, mock_slack_client):
    """Busy printer: no print, eyes removed, x reaction, threaded note."""
    monkeypatch.setattr(hardware_module, "try_begin_print_job", lambda debounce=False: False)

    connection = _make_connection()
    await handle_message_event(connection, _message_body(), mock_slack_client, AsyncMock())

    added = [call.kwargs["name"] for call in mock_slack_client.reactions_add.call_args_list]
    removed = [call.kwargs["name"] for call in mock_slack_client.reactions_remove.call_args_list]
    assert added == ["eyes", "x"]
    assert removed == ["eyes"]
    mock_slack_client.chat_postMessage.assert_awaited_once()
    mock_printer.print_header.assert_not_called()


@pytest.mark.asyncio
async def test_print_message_ignores_bot_and_edited_messages(mock_slack_client, mock_printer):
    connection = _make_connection()

    await handle_message_event(
        connection, _message_body(bot_id="B999"), mock_slack_client, AsyncMock()
    )
    await handle_message_event(
        connection, _message_body(subtype="message_changed"), mock_slack_client, AsyncMock()
    )

    mock_slack_client.reactions_add.assert_not_called()
    mock_printer.print_header.assert_not_called()


@pytest.mark.asyncio
async def test_print_message_respects_auto_print_toggle(mock_slack_client, mock_printer):
    connection = _make_connection(auto_print_messages=False)

    await handle_message_event(connection, _message_body(), mock_slack_client, AsyncMock())

    mock_slack_client.reactions_add.assert_not_called()
    mock_printer.print_header.assert_not_called()


@pytest.mark.asyncio
async def test_print_message_enforces_allow_list(mock_slack_client, mock_printer):
    connection = _make_connection(allowed_user_ids="U999, U888")

    await handle_message_event(connection, _message_body(), mock_slack_client, AsyncMock())

    added = [call.kwargs["name"] for call in mock_slack_client.reactions_add.call_args_list]
    assert added == ["no_entry"]
    mock_printer.print_header.assert_not_called()


@pytest.mark.asyncio
async def test_print_failure_clears_reservation_and_reacts(monkeypatch, mock_printer, mock_slack_client):
    cleared = []
    monkeypatch.setattr(hardware_module, "try_begin_print_job", lambda debounce=False: True)
    monkeypatch.setattr(
        hardware_module,
        "clear_print_reservation",
        lambda clear_hold=True: cleared.append(clear_hold),
    )
    monkeypatch.setattr(
        slack_manager, "get_slack_user_name", AsyncMock(return_value="TestUser")
    )
    monkeypatch.setattr(slack_manager, "extract_images", AsyncMock(return_value=[]))
    mock_printer.print_header.side_effect = RuntimeError("printer exploded")

    connection = _make_connection()
    await handle_message_event(connection, _message_body(), mock_slack_client, AsyncMock())

    assert cleared == [False]
    added = [call.kwargs["name"] for call in mock_slack_client.reactions_add.call_args_list]
    assert added == ["eyes", "x"]


# --- SLASH COMMANDS ---


def _fake_settings_with_channels(monkeypatch):
    modules = {
        "news-1": ModuleInstance(id="news-1", type="news", name="Morning News", config={}),
        "weather-1": ModuleInstance(id="weather-1", type="weather", name="Weather", config={}),
    }
    channels = {
        1: types.SimpleNamespace(
            modules=[
                types.SimpleNamespace(module_id="news-1", order=0),
                types.SimpleNamespace(module_id="weather-1", order=1),
            ]
        ),
        2: types.SimpleNamespace(modules=[]),
    }
    fake_settings = types.SimpleNamespace(
        modules=modules, channels=channels, max_print_lines=200
    )
    monkeypatch.setattr(config, "settings", fake_settings)
    return fake_settings


@pytest.mark.asyncio
async def test_channels_command_lists_modules(monkeypatch):
    _fake_settings_with_channels(monkeypatch)
    ack, respond = AsyncMock(), AsyncMock()

    await handle_channels_command(ack, respond)

    ack.assert_awaited_once()
    listing = respond.call_args.args[0]
    assert "1: Morning News + Weather" in listing
    assert "2: (empty)" in listing
    assert "8: (empty)" in listing


@pytest.mark.asyncio
async def test_channel_command_validates_input(monkeypatch):
    _fake_settings_with_channels(monkeypatch)
    respond = AsyncMock()

    await handle_channel_command(AsyncMock(), respond, {"text": "nope"})
    assert "Invalid channel number" in respond.call_args.args[0]

    await handle_channel_command(AsyncMock(), respond, {"text": "2"})
    assert "empty" in respond.call_args.args[0]


@pytest.mark.asyncio
async def test_channel_command_triggers_print(monkeypatch):
    _fake_settings_with_channels(monkeypatch)
    triggered = []

    async def fake_trigger(position):
        triggered.append(position)

    slack_manager.init_runtime(fake_trigger)
    monkeypatch.setattr(hardware_module, "try_begin_print_job", lambda debounce=False: True)

    respond = AsyncMock()
    await handle_channel_command(AsyncMock(), respond, {"text": "1"})

    assert triggered == [1]
    assert "Printing channel 1" in respond.call_args.args[0]


@pytest.mark.asyncio
async def test_channel_command_reports_busy_printer(monkeypatch):
    _fake_settings_with_channels(monkeypatch)
    triggered = []

    async def fake_trigger(position):
        triggered.append(position)

    slack_manager.init_runtime(fake_trigger)
    monkeypatch.setattr(hardware_module, "try_begin_print_job", lambda debounce=False: False)

    respond = AsyncMock()
    await handle_channel_command(AsyncMock(), respond, {"text": "1"})

    assert triggered == []
    assert "busy" in respond.call_args.args[0]


# --- MANAGER RECONCILE / DEDUPE ---


def _slack_module(module_id: str, bot="xoxb-a", app_tok="xapp-a", **cfg):
    return ModuleInstance(
        id=module_id,
        type="slack",
        name=f"Slack {module_id}",
        config={"bot_token": bot, "app_token": app_tok, **cfg},
    )


def _install_fake_settings(monkeypatch, modules):
    fake_settings = types.SimpleNamespace(modules=modules, channels={}, max_print_lines=200)
    monkeypatch.setattr(config, "settings", fake_settings)
    return fake_settings


def test_desired_instances_dedupes_shared_app_tokens(monkeypatch):
    _install_fake_settings(
        monkeypatch,
        {
            "a-first": _slack_module("a-first"),
            "b-dupe": _slack_module("b-dupe"),  # same tokens as a-first
            "c-other": _slack_module("c-other", bot="xoxb-b", app_tok="xapp-b"),
            "d-empty": _slack_module("d-empty", bot="", app_tok=""),
        },
    )

    desired = slack_manager.desired_instances()

    assert set(desired.keys()) == {"xapp-a", "xapp-b"}
    assert desired["xapp-a"][0] == "a-first"  # first by module id wins
    statuses = slack_manager._statuses
    assert statuses["b-dupe"]["status"] == "duplicate"
    assert statuses["d-empty"]["status"] == "unconfigured"


@pytest.mark.asyncio
async def test_reconcile_starts_stops_and_restarts_connections(monkeypatch):
    started, stopped = [], []

    async def fake_start(self):
        self.status = "connected"
        started.append(self.module_id)

    async def fake_stop(self, *, set_status=True):
        stopped.append(self.module_id)
        if set_status:
            self.status = "disconnected"

    monkeypatch.setattr(SlackConnection, "start", fake_start)
    monkeypatch.setattr(SlackConnection, "stop", fake_stop)

    modules = {"a": _slack_module("a")}
    _install_fake_settings(monkeypatch, modules)

    await slack_manager.reconcile()
    assert started == ["a"]
    assert slack_manager.get_module_status("a")["status"] == "connected"

    # No change -> no reconnect
    await slack_manager.reconcile()
    assert started == ["a"]

    # Token change -> stop and restart
    modules["a"] = _slack_module("a", bot="xoxb-new", app_tok="xapp-new")
    await slack_manager.reconcile()
    assert stopped == ["a"]
    assert started == ["a", "a"]

    # Module removed -> stop and clear status
    modules.pop("a")
    await slack_manager.reconcile()
    assert stopped == ["a", "a"]
    assert slack_manager.get_module_status("a")["status"] == "disconnected"
    assert not slack_manager._connections


@pytest.mark.asyncio
async def test_reconcile_retries_errored_connections(monkeypatch):
    attempts = []

    async def fake_start(self):
        attempts.append(self.module_id)
        self.status = "error"
        self.detail = "invalid_auth"

    async def fake_stop(self, *, set_status=True):
        pass

    monkeypatch.setattr(SlackConnection, "start", fake_start)
    monkeypatch.setattr(SlackConnection, "stop", fake_stop)
    _install_fake_settings(monkeypatch, {"a": _slack_module("a")})

    await slack_manager.reconcile()
    await slack_manager.reconcile()

    assert attempts == ["a", "a"]
    assert slack_manager.get_module_status("a")["status"] == "error"


# --- STATUS RECEIPT ---


class _RecordingPrinter:
    def __init__(self):
        self.lines = []

    def print_header(self, text, icon=None, **kwargs):
        self.lines.append(f"HEADER:{text}")

    def print_caption(self, text):
        self.lines.append(f"CAPTION:{text}")

    def print_body(self, text):
        self.lines.append(f"BODY:{text}")

    def print_line(self):
        self.lines.append("LINE")


def test_status_receipt_unconfigured():
    printer = _RecordingPrinter()
    format_slack_receipt(printer, {}, module_name="SLACK", module_id="missing")

    assert any("Not configured" in line for line in printer.lines)


def test_status_receipt_connected(monkeypatch):
    slack_manager._statuses["slack-1"] = {
        "status": "connected",
        "detail": "",
        "team": "Acme",
        "bot": "pc-1",
    }
    printer = _RecordingPrinter()
    format_slack_receipt(
        printer,
        {"bot_token": "xoxb-x", "app_token": "xapp-x", "auto_print_messages": True},
        module_name="WORK SLACK",
        module_id="slack-1",
    )

    joined = "\n".join(printer.lines)
    assert "HEADER:WORK SLACK" in joined
    assert "Status: Connected" in joined
    assert "Workspace: Acme" in joined
    assert "Bot: @pc-1" in joined
    assert "DM the bot to print" in joined


def test_status_receipt_error(monkeypatch):
    slack_manager._statuses["slack-2"] = {
        "status": "error",
        "detail": "Token was rejected by Slack (invalid_auth).",
        "team": "",
        "bot": "",
    }
    printer = _RecordingPrinter()
    format_slack_receipt(
        printer,
        {"bot_token": "xoxb-x", "app_token": "xapp-x"},
        module_name="SLACK",
        module_id="slack-2",
    )

    joined = "\n".join(printer.lines)
    assert "Status: Connection error" in joined
    assert "invalid_auth" in joined
    assert "Check tokens" in joined
