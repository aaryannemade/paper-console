import logging
from typing import Any, Dict, Optional

from app.config import SlackConfig, format_print_datetime
from app.drivers.printer_raster import RasterPrinter
from app.module_registry import register_module

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    "connected": "Connected",
    "connecting": "Connecting...",
    "error": "Connection error",
    "disconnected": "Not connected",
    "duplicate": "Duplicate tokens",
    "unconfigured": "Not configured",
}


@register_module(
    type_id="slack",
    label="Slack",
    description="Print Slack DMs, links, and images sent to your bot",
    icon="slack-logo",
    offline=False,
    category="utilities",
    config_schema={
        "type": "object",
        "description": (
            "Connect a Slack workspace. Messages sent to your bot are printed "
            "as they arrive, even when this module is not on the active channel."
        ),
        "properties": {
            "bot_token": {
                "type": "string",
                "title": "Bot User OAuth Token",
                "description": "Starts with xoxb-. Found under OAuth & Permissions.",
            },
            "app_token": {
                "type": "string",
                "title": "App-Level Token",
                "description": "Starts with xapp-. Created under Basic Information with the connections:write scope.",
            },
            "auto_print_messages": {
                "type": "boolean",
                "title": "Print incoming messages",
                "description": "Print DMs sent to the bot as they arrive.",
                "default": True,
            },
            "allowed_user_ids": {
                "type": "string",
                "title": "Allowed Slack User IDs",
                "description": "Optional comma-separated Slack member IDs (e.g. U0123ABC). Leave blank to allow everyone in the workspace.",
            },
            "setup_help": {"type": "null", "title": ""},
        },
    },
    ui_schema={
        "bot_token": {"ui:widget": "password", "ui:placeholder": "xoxb-..."},
        "app_token": {"ui:widget": "password", "ui:placeholder": "xapp-..."},
        "allowed_user_ids": {"ui:placeholder": "U0123ABC, U0456DEF"},
        "setup_help": {"ui:widget": "slack-help"},
    },
)
def format_slack_receipt(
    printer: RasterPrinter,
    config: Optional[Dict[str, Any]] = None,
    module_name: str = None,
    module_id: str = None,
) -> None:
    """Status receipt for the print button: connection state and usage hint."""
    if isinstance(config, SlackConfig):
        cfg = config
    else:
        cfg = SlackConfig(**(config or {}))
    module_name = module_name or "SLACK"

    printer.print_header(module_name, icon="slack-logo")
    printer.print_caption(format_print_datetime())
    printer.print_line()

    if not cfg.bot_token.strip() or not cfg.app_token.strip():
        printer.print_body("Not configured.")
        printer.print_caption("Add your Slack bot + app tokens")
        printer.print_caption("in the web settings to connect.")
        return

    status = _lookup_status(module_id)
    state = status.get("status", "disconnected")
    printer.print_body(f"Status: {STATUS_LABELS.get(state, state)}")

    if status.get("team"):
        printer.print_caption(f"Workspace: {status['team']}")
    if status.get("bot"):
        printer.print_caption(f"Bot: @{status['bot']}")
    if status.get("detail"):
        printer.print_caption(str(status["detail"]))

    printer.print_line()
    if state == "connected":
        if cfg.auto_print_messages:
            printer.print_body("DM the bot to print a message.")
        else:
            printer.print_caption("Incoming message printing is off.")
        printer.print_caption("/channels lists dial channels.")
        printer.print_caption("/channel N prints channel N.")
    else:
        printer.print_caption("Check tokens in web settings,")
        printer.print_caption("then save to reconnect.")


def _lookup_status(module_id: Optional[str]) -> Dict[str, Any]:
    if not module_id:
        return {}
    try:
        from app import slack_manager

        return slack_manager.get_module_status(module_id)
    except Exception:  # noqa: BLE001
        logger.debug("Could not read Slack status", exc_info=True)
        return {}
