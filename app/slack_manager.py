"""
Slack integration manager.

Each Slack module instance represents one Slack workspace (one Slack app with
its own bot token + app-level token). This manager keeps one Socket Mode
connection per unique app token and reconciles running connections against the
current settings whenever modules change.

Workspace setup (shown in the module editor too):
1. Create a Slack app (https://api.slack.com/apps) for your workspace.
2. Enable Socket Mode and create an App-Level Token with `connections:write`.
3. Under OAuth & Permissions add bot scopes: `chat:write`, `im:history`,
   `reactions:write`, `users:read`, `files:read`, then install the app.
4. Under Event Subscriptions -> Subscribe to bot events add `message.im`.
5. (Optional) Create slash commands `/channels` and `/channel`.
6. Paste the Bot User OAuth Token (xoxb-...) and App-Level Token (xapp-...)
   into the Slack module in PC-1 settings.

Incoming DMs (text, links, images) are printed as they arrive. Links are
printed as QR codes. `/channels` lists the dial channels; `/channel N`
triggers a print of channel N.
"""

import asyncio
import logging
import re
from io import BytesIO
from typing import Any, Awaitable, Callable, Dict, List, Optional

import app.config as config
from app import hardware
from app.config import SlackConfig, format_print_datetime

logger = logging.getLogger(__name__)

SLACK_IMAGE_MAX_WIDTH_DOTS = 384
SLACK_IMAGE_MAX_HEIGHT_DOTS = 4096
SLACK_IMAGE_MAX_BYTES = 5 * 1024 * 1024

# Injected from app.main at startup to avoid a circular import.
_trigger_channel: Optional[Callable[..., Awaitable[None]]] = None

# Active connections keyed by app token (one Socket Mode socket per Slack app).
_connections: Dict[str, "SlackConnection"] = {}

# Last reported status per module id (survives failed connections so the UI
# and status receipt can explain what is wrong).
_statuses: Dict[str, Dict[str, Any]] = {}

_reconcile_lock = asyncio.Lock()


class SlackConnection:
    """One live Socket Mode connection for one Slack workspace."""

    def __init__(self, module_id: str, module_name: str, cfg: SlackConfig):
        self.module_id = module_id
        self.module_name = module_name
        self.cfg = cfg
        self.app = None  # slack_bolt AsyncApp
        self.handler = None  # AsyncSocketModeHandler
        self.status = "connecting"
        self.detail = ""
        self.team_name = ""
        self.bot_name = ""

    @property
    def signature(self) -> tuple:
        """Config fingerprint used to detect changes that require a reconnect."""
        return (self.cfg.bot_token, self.cfg.app_token)

    async def start(self):
        try:
            from slack_bolt.app.async_app import AsyncApp
            from slack_bolt.adapter.socket_mode.async_handler import (
                AsyncSocketModeHandler,
            )
        except ImportError:
            self.status = "error"
            self.detail = "slack_bolt is not installed"
            logger.error(
                "Slack module %s is configured but slack_bolt is not installed.",
                self.module_id,
            )
            return

        try:
            self.app = AsyncApp(
                token=self.cfg.bot_token,
                # Signing-secret verification does not apply to Socket Mode.
                request_verification_enabled=False,
            )
            self._register_handlers()

            auth = await self.app.client.auth_test()
            self.team_name = auth.get("team", "") or ""
            self.bot_name = auth.get("user", "") or ""

            self.handler = AsyncSocketModeHandler(self.app, self.cfg.app_token)
            await self.handler.connect_async()
            self.status = "connected"
            self.detail = ""
            logger.info(
                "Slack module %s connected to workspace '%s' as @%s",
                self.module_id,
                self.team_name,
                self.bot_name,
            )
        except Exception as exc:  # noqa: BLE001
            self.status = "error"
            self.detail = _friendly_slack_error(exc)
            logger.error(
                "Slack module %s failed to connect: %s", self.module_id, exc
            )
            await self.stop(set_status=False)

    async def stop(self, *, set_status: bool = True):
        if self.handler is not None:
            try:
                await self.handler.close_async()
            except Exception:  # noqa: BLE001
                logger.debug("Error closing Slack socket handler", exc_info=True)
            self.handler = None
        self.app = None
        if set_status:
            self.status = "disconnected"

    def _register_handlers(self):
        connection = self

        @self.app.event("message")
        async def _on_message(body, client, ack):  # noqa: ANN001
            await handle_message_event(connection, body, client, ack)

        @self.app.command("/channels")
        async def _on_channels(ack, respond):  # noqa: ANN001
            await handle_channels_command(ack, respond)

        @self.app.command("/channel")
        async def _on_channel(ack, respond, command):  # noqa: ANN001
            await handle_channel_command(ack, respond, command)


def init_runtime(trigger_channel: Callable[..., Awaitable[None]]):
    """Inject the channel trigger coroutine from app.main."""
    global _trigger_channel
    _trigger_channel = trigger_channel


def desired_instances() -> Dict[str, SlackConfig]:
    """Map of app_token -> (module_id, module_name, SlackConfig) to run.

    The first instance (by module id) wins for a duplicated app token; the
    duplicates are reported via status instead of opening a second socket.
    """
    desired: Dict[str, tuple] = {}
    settings = config.settings
    modules = getattr(settings, "modules", {}) or {}

    for module_id in sorted(modules.keys()):
        module = modules[module_id]
        if getattr(module, "type", None) != "slack":
            continue

        cfg = SlackConfig(**(module.config or {}))
        name = getattr(module, "name", "") or "SLACK"

        if not cfg.bot_token.strip() or not cfg.app_token.strip():
            _statuses[module_id] = {
                "status": "unconfigured",
                "detail": "Bot token and app token are required.",
                "team": "",
                "bot": "",
            }
            continue

        token = cfg.app_token.strip()
        if token in desired:
            other_id = desired[token][0]
            _statuses[module_id] = {
                "status": "duplicate",
                "detail": f"Same app token as module {other_id}; not connecting twice.",
                "team": "",
                "bot": "",
            }
            logger.warning(
                "Slack modules %s and %s share an app token; only %s will connect.",
                other_id,
                module_id,
                other_id,
            )
            continue

        desired[token] = (module_id, name, cfg)

    return desired


async def reconcile():
    """Start/stop/restart Slack connections to match the current settings."""
    async with _reconcile_lock:
        desired = desired_instances()
        desired_module_ids = {entry[0] for entry in desired.values()}

        # Drop stale statuses for modules that no longer exist.
        settings_modules = getattr(config.settings, "modules", {}) or {}
        for module_id in list(_statuses.keys()):
            if module_id not in settings_modules:
                _statuses.pop(module_id, None)

        # Stop connections that are gone or whose config changed.
        for token in list(_connections.keys()):
            conn = _connections[token]
            entry = desired.get(token)
            if (
                entry is None
                or entry[0] != conn.module_id
                or (entry[2].bot_token, entry[2].app_token) != conn.signature
            ):
                await conn.stop()
                _connections.pop(token, None)
                if conn.module_id not in desired_module_ids:
                    _statuses.pop(conn.module_id, None)

        # Start missing connections; retry ones that previously errored.
        for token, (module_id, name, cfg) in desired.items():
            conn = _connections.get(token)
            if conn is None:
                conn = SlackConnection(module_id, name, cfg)
                _connections[token] = conn
                await conn.start()
            elif conn.status == "error":
                conn.module_name = name
                conn.cfg = cfg
                await conn.stop(set_status=False)
                await conn.start()
            else:
                # Refresh non-connection config (name, auto-print, allow-list).
                conn.module_name = name
                conn.cfg = cfg
            _publish_status(conn)


def _publish_status(conn: SlackConnection):
    _statuses[conn.module_id] = {
        "status": conn.status,
        "detail": conn.detail,
        "team": conn.team_name,
        "bot": conn.bot_name,
    }


async def shutdown():
    """Close all connections (app shutdown)."""
    for conn in list(_connections.values()):
        await conn.stop()
    _connections.clear()


def get_statuses() -> Dict[str, Dict[str, Any]]:
    """Per-module-id connection status (for /api/health and the status receipt)."""
    for conn in _connections.values():
        _publish_status(conn)
    return dict(_statuses)


def get_module_status(module_id: str) -> Dict[str, Any]:
    return get_statuses().get(
        module_id,
        {"status": "disconnected", "detail": "", "team": "", "bot": ""},
    )


async def test_credentials(bot_token: str, app_token: str) -> Dict[str, Any]:
    """Validate a token pair without touching the running connections."""
    bot_token = (bot_token or "").strip()
    app_token = (app_token or "").strip()
    if not bot_token or not app_token:
        return {"ok": False, "error": "Both tokens are required."}

    try:
        from slack_sdk.web.async_client import AsyncWebClient
    except ImportError:
        return {"ok": False, "error": "slack_bolt / slack_sdk is not installed on this device."}

    result: Dict[str, Any] = {"ok": True}
    try:
        auth = await AsyncWebClient(token=bot_token).auth_test()
        result["team"] = auth.get("team", "")
        result["bot"] = auth.get("user", "")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Bot token: {_friendly_slack_error(exc)}"}

    try:
        await AsyncWebClient().apps_connections_open(app_token=app_token)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"App token: {_friendly_slack_error(exc)}"}

    return result


def _friendly_slack_error(exc: Exception) -> str:
    message = str(exc)
    match = re.search(r"'error': '([^']+)'", message)
    if match:
        code = match.group(1)
        translations = {
            "invalid_auth": "Token was rejected by Slack (invalid_auth).",
            "not_authed": "Token missing or malformed (not_authed).",
            "account_inactive": "Slack app or workspace is inactive.",
            "token_revoked": "Token has been revoked.",
            "missing_scope": "Token is missing a required scope.",
        }
        return translations.get(code, code)
    return message[:200]


# --- INBOUND MESSAGE HANDLING ---


def _parse_allowed_user_ids(raw: str) -> List[str]:
    return [part.strip() for part in re.split(r"[,\s]+", raw or "") if part.strip()]


async def handle_message_event(connection: SlackConnection, body, client, ack):
    """Print a DM sent to the bot."""
    await ack()
    event = body.get("event", {}) or {}

    # Ignore bot echoes, edits, deletions, joins, etc. Only fresh user DMs.
    if event.get("bot_id") or event.get("subtype"):
        return
    if event.get("channel_type") not in (None, "im"):
        return

    if not connection.cfg.auto_print_messages:
        logger.info(
            "Ignoring Slack DM for module %s: auto-print is disabled.",
            connection.module_id,
        )
        return

    allowed = _parse_allowed_user_ids(connection.cfg.allowed_user_ids)
    sender = event.get("user")
    if allowed and sender not in allowed:
        logger.info(
            "Ignoring Slack DM from %s: not in the allow-list for module %s.",
            sender,
            connection.module_id,
        )
        await _react(client, event, "no_entry")
        return

    await _react(client, event, "eyes")

    if not hardware.try_begin_print_job(debounce=False):
        logger.info("Skipping Slack message print: printer is busy.")
        await _unreact(client, event, "eyes")
        await _react(client, event, "x")
        await _post_threaded_note(
            client, event, "Printer is busy right now - send that again in a moment."
        )
        return

    try:
        user_name = await get_slack_user_name(client, sender)
        blocks = event.get("blocks", []) or []
        urls = extract_urls(blocks)
        images = await extract_images(event, client.token)
        content_doc = slack_to_tiptap(blocks, urls)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            _print_slack_message_sync,
            connection.module_name,
            user_name,
            content_doc,
            urls,
            images,
        )
    except Exception:
        logger.exception("Failed to print Slack message for module %s", connection.module_id)
        await _unreact(client, event, "eyes")
        await _react(client, event, "x")
        return
    finally:
        hardware.clear_print_reservation(clear_hold=False)

    await _unreact(client, event, "eyes")
    await _react(client, event, "printer")


def _print_slack_message_sync(
    module_name: str,
    user_name: str,
    content_doc: Dict[str, Any],
    urls: List[str],
    images: List[Any],
):
    """Blocking receipt print for one Slack message (runs in an executor)."""
    from app.modules.text import print_rich_doc

    printer = hardware.printer
    if hasattr(printer, "reset_buffer"):
        max_lines = getattr(config.settings, "max_print_lines", 200)
        printer.reset_buffer(max_lines)

    printer.print_header(module_name or "SLACK", icon="slack-logo")
    printer.print_caption(f"From: {user_name}")
    printer.print_caption(format_print_datetime())
    printer.print_line()

    print_rich_doc(printer, content_doc)

    # Print QR codes for any links found in the message.
    for i, url in enumerate(urls, 1):
        printer.feed(2)
        qr_url = url
        if not qr_url.startswith(("http://", "https://")):
            qr_url = "https://" + qr_url

        printer.print_body(f"[{i}]")
        printer.print_qr(
            data=qr_url,
            # Highest error correction maximizes scanability from paper.
            error_correction="H",
        )

    for image in images:
        printer.feed(2)
        printer.print_image(image)

    if hasattr(printer, "flush_buffer"):
        printer.flush_buffer()


async def _react(client, event, name: str):
    try:
        await client.reactions_add(
            channel=event.get("channel"),
            timestamp=event.get("ts"),
            name=name,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Failed to add Slack reaction %s", name, exc_info=True)


async def _unreact(client, event, name: str):
    try:
        await client.reactions_remove(
            channel=event.get("channel"),
            timestamp=event.get("ts"),
            name=name,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Failed to remove Slack reaction %s", name, exc_info=True)


async def _post_threaded_note(client, event, text: str):
    try:
        await client.chat_postMessage(
            channel=event.get("channel"),
            thread_ts=event.get("ts"),
            text=text,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Failed to post Slack note", exc_info=True)


async def get_slack_user_name(client, user_id) -> str:
    if not user_id:
        return "UNKNOWN"
    try:
        response = await client.users_info(user=user_id)
        if response and response.data:
            user = response.data.get("user", {}) or {}
            return user.get("real_name") or user.get("name") or "UNKNOWN"
        return "UNKNOWN"
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to fetch Slack user info for %s: %s", user_id, exc)
        return "UNKNOWN"


async def extract_images(event, token) -> List[Any]:
    """Download and prepare image attachments from a Slack message event."""
    from PIL import Image

    images: List[Any] = []
    image_files = [
        f
        for f in event.get("files", []) or []
        if (f.get("mimetype") or "").startswith("image/")
    ]
    if not image_files:
        return images

    import aiohttp

    async with aiohttp.ClientSession(
        headers={"Authorization": f"Bearer {token}"}
    ) as session:
        for file in image_files:
            try:
                url = file.get("url_private_download")
                if not url:
                    continue
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"Download failed with status {resp.status}")
                    image_data = await resp.read()
                    if len(image_data) > SLACK_IMAGE_MAX_BYTES:
                        raise RuntimeError("Image attachment is too large to print.")
                    image = Image.open(BytesIO(image_data))
                    images.append(prepare_image_for_print(image))
            except Exception as exc:  # noqa: BLE001
                logger.error("Unable to download and process Slack image: %s", exc)
                continue
    return images


def prepare_image_for_print(image):
    """Resize to receipt width and convert to 1-bit dithered for thermal print."""
    from PIL import Image

    image = image.copy()
    image.thumbnail(
        (SLACK_IMAGE_MAX_WIDTH_DOTS, SLACK_IMAGE_MAX_HEIGHT_DOTS),
        Image.Resampling.LANCZOS,
    )
    return image.convert("1")


# --- SLASH COMMANDS ---


async def handle_channels_command(ack, respond):
    """`/channels` - list the dial channels and their assigned modules."""
    await ack()

    settings = config.settings
    lines = ["*PC-1 Channels*"]
    for channel_num in range(1, 9):
        channel = settings.channels.get(channel_num)
        names: List[str] = []
        if channel and channel.modules:
            for assignment in sorted(channel.modules, key=lambda m: m.order):
                module = settings.modules.get(assignment.module_id)
                if module:
                    names.append(module.name)
        if names:
            lines.append(f"{channel_num}: {' + '.join(names)}")
        else:
            lines.append(f"{channel_num}: (empty)")

    await respond("\n".join(lines))


async def handle_channel_command(ack, respond, command):
    """`/channel N` - trigger a print of dial channel N."""
    await ack()

    raw = (command.get("text") or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 8):
        await respond(f"Invalid channel number: '{raw}'. Use a number between 1 and 8.")
        return

    channel_num = int(raw)
    settings = config.settings
    channel = settings.channels.get(channel_num)
    if not channel or not channel.modules:
        await respond(f"Channel {channel_num} is empty.")
        return

    if _trigger_channel is None:
        await respond("PC-1 is still starting up - try again in a moment.")
        return

    if not hardware.try_begin_print_job(debounce=False):
        await respond(f"Cannot print channel {channel_num} right now: printer is busy.")
        return

    await respond(f"Printing channel {channel_num}...")
    # trigger_channel clears the print reservation in its own finally block.
    await _trigger_channel(channel_num)


# --- SLACK BLOCKS -> TIPTAP CONVERSION ---


def extract_urls(slack_blocks) -> List[str]:
    """Recursively extract unique URLs from Slack blocks."""
    urls: List[str] = []

    def _walk(elements):
        for el in elements:
            if el.get("type") == "link":
                url = el.get("url")
                if url and url not in urls:
                    urls.append(url)
            if "elements" in el:
                _walk(el["elements"])

    for block in slack_blocks or []:
        if "elements" in block:
            _walk(block["elements"])
    return urls


def slack_to_tiptap(slack_blocks, urls=None) -> Dict[str, Any]:
    """
    Convert Slack Block Kit blocks to TipTap JSON.
    Handles headings, dividers, lists (bullet, ordered, task), code, and quotes.
    """
    tiptap_doc: Dict[str, Any] = {"type": "doc", "content": []}

    for block in slack_blocks or []:
        block_type = block.get("type")

        if block_type == "header":
            text = block.get("text", {}).get("text", "")
            tiptap_doc["content"].append(
                {
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": [{"type": "text", "text": text}],
                }
            )
        elif block_type == "divider":
            tiptap_doc["content"].append({"type": "horizontalRule"})
        elif block_type == "rich_text":
            for element in block.get("elements", []):
                tiptap_node = process_rich_text_element(element, urls)
                if tiptap_node:
                    if isinstance(tiptap_node, list):
                        tiptap_doc["content"].extend(tiptap_node)
                    else:
                        tiptap_doc["content"].append(tiptap_node)

    return tiptap_doc


def process_rich_text_element(element, urls=None):
    """Map Slack rich_text elements to TipTap nodes."""
    el_type = element.get("type")

    if el_type in ("rich_text_section", "rich_text_preformatted"):
        return {
            "type": "paragraph",
            "content": [
                transform_text(item, urls)
                for item in element.get("elements", [])
                if item.get("type") in ("text", "link")
            ],
        }

    if el_type == "rich_text_quote":
        # A quote can contain multiple sections; return a list of paragraphs.
        paragraphs = []
        for inner in element.get("elements", []):
            if inner.get("type") == "rich_text_section":
                paragraphs.append(
                    {
                        "type": "paragraph",
                        "content": [
                            transform_text(item, urls)
                            for item in inner.get("elements", [])
                            if item.get("type") in ("text", "link")
                        ],
                    }
                )
        return paragraphs

    if el_type == "rich_text_list":
        style = element.get("style")
        node_type = {"bullet": "bulletList", "ordered": "orderedList"}.get(
            style, "bulletList"
        )
        item_type = "listItem"
        if style == "checked":
            node_type = "taskList"
            item_type = "taskItem"

        list_content = []
        for item in element.get("elements", []):
            inner_content = [
                transform_text(t, urls)
                for t in item.get("elements", [])
                if t.get("type") in ("text", "link")
            ]
            list_item: Dict[str, Any] = {
                "type": item_type,
                "content": [{"type": "paragraph", "content": inner_content}],
            }
            if item_type == "taskItem":
                list_item["attrs"] = {"checked": True}
            list_content.append(list_item)

        return {"type": node_type, "content": list_content}

    return None


def transform_text(slack_text_obj, urls=None) -> Dict[str, Any]:
    """Convert Slack text objects and their styles to TipTap marks."""
    url = slack_text_obj.get("url")
    if url and urls and url in urls:
        # Replace the link with its printed QR reference number [N].
        idx = urls.index(url) + 1
        if slack_text_obj.get("text"):
            text = f"{slack_text_obj.get('text')}[{idx}]"
        else:
            text = f"[{idx}]"
    else:
        text = slack_text_obj.get("text") or url or ""

    styles = slack_text_obj.get("style", {}) or {}
    marks = []
    if styles.get("bold"):
        marks.append({"type": "bold"})
    if styles.get("italic"):
        marks.append({"type": "italic"})

    node: Dict[str, Any] = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node
