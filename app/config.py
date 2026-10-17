from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Optional, Dict, List, Any, Literal
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import os
import uuid
import logging

from pathlib import Path
from app.paths import data_dir

# Constants
PRINTER_WIDTH = 42
DEFAULT_CUTTER_FEED_LINES = 5
logger = logging.getLogger(__name__)
_warned_non_posix_sensitive_config = False
_warned_non_posix_sensitive_data = False


class WebhookConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str = ""
    method: str = "GET"
    headers: Dict[str, str] = {}
    body: Optional[str] = None
    json_path: Optional[str] = None
    auth_type: str = "none"
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None


class PrintWebhookConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token: str = ""
    endpoint_path: str = ""
    accept_text: bool = True
    accept_images: bool = True
    accept_json: bool = True
    print_when_channel_active: bool = True
    max_image_height_dots: int = 4096
    print_header: Optional[str] = None
    print_sender_ip: bool = False
    print_content_type: bool = False
    print_user_agent: bool = False


class EmailConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    email_host: str = "imap.gmail.com"
    email_user: Optional[str] = None
    email_password: Optional[str] = None
    polling_interval: int = 30  # Default to 30 seconds
    auto_print_new: bool = False  # Whether to automatically print new emails


class SlackConfig(BaseModel):
    """One Slack module instance = one Slack workspace (one Slack app)."""

    model_config = ConfigDict(extra="ignore")
    bot_token: str = ""  # xoxb-... (Bot User OAuth Token)
    app_token: str = ""  # xapp-... (App-Level Token for Socket Mode)
    auto_print_messages: bool = True  # Print DMs sent to the bot as they arrive
    allowed_user_ids: str = ""  # Optional comma-separated Slack user IDs allow-list


def _default_text_content_doc() -> Dict[str, Any]:
    return {"type": "doc", "content": [{"type": "paragraph"}]}


class TextConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content_doc: Dict[str, Any] = Field(default_factory=_default_text_content_doc)


class QRCodeConfig(BaseModel):
    """Configuration for QR code generation."""
    model_config = ConfigDict(extra="ignore")
    qr_type: str = "url"  # text, url, wifi, contact, phone, sms, email
    content: str = "https://example.com"  # Main content (URL, text, phone number, etc.)
    
    # WiFi-specific fields
    wifi_ssid: str = ""
    wifi_password: str = ""
    wifi_security: str = "WPA"  # WPA, WEP, or nopass
    wifi_hidden: bool = False
    
    # Contact (vCard) fields
    contact_name: str = ""
    contact_last_name: str = ""
    contact_phone: str = ""
    contact_email: str = ""
    
    # QR code display settings (fixed for optimal scannability)
    size: int = 6  # Module size - larger = easier to scan
    error_correction: str = "H"  # High error correction for durability


class CalendarSource(BaseModel):
    model_config = ConfigDict(extra="ignore")
    label: Optional[str] = None
    name: Optional[str] = None
    url: str = ""


class CalendarConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ical_sources: List[CalendarSource] = Field(default_factory=list)
    view_mode: Optional[Literal["day", "week", "month"]] = None
    days_to_show: Optional[int] = None  # Legacy fallback for older saved configs.
    mock_ics_content: Optional[str] = None



class ModuleInstance(BaseModel):
    """A module instance with its configuration."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str  # e.g., "news", "sudoku", "calendar"
    name: str = ""  # User-friendly name for the module instance
    config: Dict[str, Any] = {}


class ChannelModuleAssignment(BaseModel):
    """A module assignment within a channel with ordering."""

    model_config = ConfigDict(extra="ignore")

    module_id: str
    order: int = 0  # Order within the channel (0 = first)


class ScheduleRule(BaseModel):
    """A cron-based schedule rule, evaluated in the configured app timezone."""

    model_config = ConfigDict(extra="ignore")

    expression: str  # 5-field cron expression, e.g. "30 8 * * 1-5"
    enabled: bool = True


CHANNEL_NAME_MAX_LENGTH = 24


def normalize_channel_name(value) -> str:
    """Single-line, trimmed, at most CHANNEL_NAME_MAX_LENGTH characters."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Channel name must be a string")
    printable = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(printable.split())[:CHANNEL_NAME_MAX_LENGTH].rstrip()


class ChannelConfig(BaseModel):
    """Channel configuration - can have multiple modules assigned."""

    model_config = ConfigDict(extra="ignore")

    # Optional user label shown in the UI, on receipts and later on a device
    # display. Empty means unnamed; the position number is always the identity.
    name: str = ""

    # New format: list of module assignments
    modules: List[ChannelModuleAssignment] = []

    @field_validator("name", mode="before")
    @classmethod
    def _normalize_name(cls, value):
        return normalize_channel_name(value)

    # Legacy schedule: list of "HH:MM" times. Converted to schedule_rules on
    # first save; still honored at runtime for configs that predate cron rules.
    schedule: List[str] = []

    # Cron schedule rules (current format).
    schedule_rules: List[ScheduleRule] = []


# Default module IDs (fixed UUIDs for predictable defaults)
DEFAULT_WEATHER_ID = "default-weather-001"
DEFAULT_ASTRONOMY_ID = "default-astronomy-001"
DEFAULT_SUDOKU_ID = "default-sudoku-001"
DEFAULT_MAZE_ID = "default-maze-001"
DEFAULT_QUOTES_ID = "default-quotes-001"
DEFAULT_JOURNAL_PROMPTS_ID = "default-journal-prompts-001"
DEFAULT_HISTORY_ID = "default-history-001"
DEFAULT_TEXT_ID = "default-text-001"
DEFAULT_SYSTEM_MONITOR_ID = "default-system-monitor-001"


def _default_modules() -> Dict[str, ModuleInstance]:
    """Create default offline modules for out-of-box experience."""
    return {
        DEFAULT_WEATHER_ID: ModuleInstance(
            id=DEFAULT_WEATHER_ID,
            type="weather",
            name="Weather",
            config={},
        ),
        DEFAULT_ASTRONOMY_ID: ModuleInstance(
            id=DEFAULT_ASTRONOMY_ID,
            type="astronomy",
            name="Astronomy",
            config={},
        ),
        DEFAULT_SUDOKU_ID: ModuleInstance(
            id=DEFAULT_SUDOKU_ID,
            type="sudoku",
            name="Sudoku",
            config={"difficulty": "Medium"},
        ),
        DEFAULT_MAZE_ID: ModuleInstance(
            id=DEFAULT_MAZE_ID,
            type="maze",
            name="Maze",
            config={"difficulty": "Medium"},
        ),
        DEFAULT_QUOTES_ID: ModuleInstance(
            id=DEFAULT_QUOTES_ID,
            type="quotes",
            name="Daily Quote",
            config={},
        ),
        DEFAULT_JOURNAL_PROMPTS_ID: ModuleInstance(
            id=DEFAULT_JOURNAL_PROMPTS_ID,
            type="journal_prompts",
            name="Journal Prompt",
            config={},
        ),
        DEFAULT_HISTORY_ID: ModuleInstance(
            id=DEFAULT_HISTORY_ID,
            type="history",
            name="On This Day",
            config={"count": 1},
        ),
        DEFAULT_TEXT_ID: ModuleInstance(
            id=DEFAULT_TEXT_ID,
            type="text",
            name="Note",
            config={"content_doc": _default_text_content_doc()},
        ),
        DEFAULT_SYSTEM_MONITOR_ID: ModuleInstance(
            id=DEFAULT_SYSTEM_MONITOR_ID,
            type="system_monitor",
            name="System Monitor",
            config={},
        ),
    }


def _default_channels() -> Dict[int, ChannelConfig]:
    """Create default channel assignments for out-of-box experience."""
    return {
        1: ChannelConfig(
            modules=[
                ChannelModuleAssignment(module_id=DEFAULT_JOURNAL_PROMPTS_ID, order=0)
            ]
        ),
        2: ChannelConfig(
            modules=[ChannelModuleAssignment(module_id=DEFAULT_QUOTES_ID, order=0)]
        ),
        3: ChannelConfig(
            modules=[ChannelModuleAssignment(module_id=DEFAULT_HISTORY_ID, order=0)]
        ),
        4: ChannelConfig(
            modules=[ChannelModuleAssignment(module_id=DEFAULT_ASTRONOMY_ID, order=0)]
        ),
        5: ChannelConfig(
            modules=[ChannelModuleAssignment(module_id=DEFAULT_SUDOKU_ID, order=0)]
        ),
        6: ChannelConfig(
            modules=[ChannelModuleAssignment(module_id=DEFAULT_MAZE_ID, order=0)]
        ),
        7: ChannelConfig(
            modules=[ChannelModuleAssignment(module_id=DEFAULT_TEXT_ID, order=0)]
        ),
        8: ChannelConfig(
            modules=[ChannelModuleAssignment(module_id=DEFAULT_SYSTEM_MONITOR_ID, order=0)]
        ),
    }


class Settings(BaseModel):
    timezone: str = "America/New_York"
    latitude: float = 40.7128
    longitude: float = -74.0060
    city_name: str = "New York"
    state: Optional[str] = None
    time_format: str = "12h"  # "12h" for 12-hour format, "24h" for 24-hour format
    max_print_lines: int = 200  # Maximum lines per print job (0 = no limit)
    theme: str = "light"  # Web UI theme: "light", "dark", or "system"

    # Module Instances: Dictionary of module_id -> ModuleInstance
    # These are reusable module configurations that can be assigned to channels
    # Default includes offline modules for out-of-box experience
    modules: Dict[str, ModuleInstance] = Field(default_factory=_default_modules)

    # Universal Channel Configuration
    # Key is position (1-8), Value is the configuration for that channel
    # Default: Pre-populated offline-friendly modules across all 8 channels
    channels: Dict[int, ChannelConfig] = Field(default_factory=_default_channels)

    @field_validator("latitude")
    @classmethod
    def validate_latitude(cls, v):
        if not -90 <= v <= 90:
            raise ValueError("Latitude must be between -90 and 90")
        return v

    @field_validator("longitude")
    @classmethod
    def validate_longitude(cls, v):
        if not -180 <= v <= 180:
            raise ValueError("Longitude must be between -180 and 180")
        return v

    @field_validator("time_format")
    @classmethod
    def validate_time_format(cls, v):
        if v not in ("12h", "24h"):
            return "12h"
        return v

    @field_validator("theme")
    @classmethod
    def validate_theme(cls, v):
        if v not in ("light", "dark", "system"):
            return "light"
        return v

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v):
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            return "America/New_York"
        return v

    # Ignore extra fields (allows old configs with removed settings to still load)
    model_config = ConfigDict(extra="ignore")


def get_config_dir() -> Path:
    """Where settings and their backup live (project root locally, /data in the image)."""
    return data_dir(Path(__file__).resolve().parents[1])


def _try_load_config_file(config_path: str) -> Settings | None:
    """Attempt to load config from a specific file path. Returns None on failure."""
    if not os.path.exists(config_path):
        return None

    try:
        with open(config_path, "r") as f:
            data = json.load(f)

            # Check if channel 1 is a string (very old format)
            channels = data.get("channels", {})
            if channels and isinstance(channels.get("1"), str):
                return Settings()

            # Normalize channel keys to integers (JSON loads them as strings)
            if "channels" in data and data["channels"]:
                normalized_channels = {}
                for key, value in data["channels"].items():
                    try:
                        normalized_channels[int(key)] = value
                    except (ValueError, TypeError):
                        normalized_channels[key] = value
                data["channels"] = normalized_channels

            return Settings(**data)
    except Exception:
        return None


def load_config() -> Settings:
    """Load settings from config.json or backup, or return defaults."""
    base_dir = str(get_config_dir())
    config_path = os.path.join(base_dir, "config.json")
    backup_path = os.path.join(base_dir, "config.json.bak")

    # Try main config first
    settings = _try_load_config_file(config_path)
    if settings is not None:
        return settings

    # Main config failed, try backup
    if os.path.exists(backup_path):
        settings = _try_load_config_file(backup_path)
        if settings is not None:
            try:
                save_config(settings)
            except Exception:
                pass
            return settings

    # Both failed, return defaults
    return Settings()


def save_config(new_settings: Settings):
    """Saves the settings object to config.json using atomic write to prevent corruption."""
    base_dir = str(get_config_dir())
    os.makedirs(base_dir, exist_ok=True)
    config_path = os.path.join(base_dir, "config.json")
    backup_path = os.path.join(base_dir, "config.json.bak")
    temp_path = os.path.join(base_dir, "config.json.tmp")

    def _contains_sensitive_config_values(value: Any) -> bool:
        """Best-effort scan for likely secrets in config payloads."""
        sensitive_markers = ("password", "token", "secret", "api_key", "apikey")

        if isinstance(value, dict):
            for key, subval in value.items():
                key_text = str(key).lower()
                if any(marker in key_text for marker in sensitive_markers):
                    if isinstance(subval, str) and subval.strip():
                        return True
                if _contains_sensitive_config_values(subval):
                    return True
            return False

        if isinstance(value, list):
            return any(_contains_sensitive_config_values(item) for item in value)

        return False

    def _restrict_file_permissions(path: str):
        """Keep config files owner-only readable on POSIX systems."""
        global _warned_non_posix_sensitive_config
        if os.name != "posix":
            # We cannot enforce chmod semantics on non-POSIX from here.
            if not _warned_non_posix_sensitive_config:
                logger.warning(
                    "Config permission hardening (chmod 600) is only enforced on POSIX systems."
                )
                _warned_non_posix_sensitive_config = True
            return
        try:
            os.chmod(path, 0o600)
        except Exception:
            logger.warning("Failed to apply secure permissions to %s", path, exc_info=True)

    try:
        # Write to temp file first
        with open(temp_path, "w") as f:
            settings_dict = new_settings.model_dump(exclude_unset=False)
            global _warned_non_posix_sensitive_data
            if (
                _contains_sensitive_config_values(settings_dict)
                and os.name != "posix"
                and not _warned_non_posix_sensitive_data
            ):
                logger.warning(
                    "Config includes sensitive fields but OS-level file mode hardening is unavailable on this platform."
                )
                _warned_non_posix_sensitive_data = True
            json.dump(settings_dict, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        _restrict_file_permissions(temp_path)

        # Backup existing config
        if os.path.exists(config_path):
            try:
                if os.path.exists(backup_path):
                    os.remove(backup_path)
                os.rename(config_path, backup_path)
                _restrict_file_permissions(backup_path)
            except Exception:
                pass

        # Atomic rename
        os.rename(temp_path, config_path)
        _restrict_file_permissions(config_path)

    except Exception:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise


def format_time(dt: datetime, time_format: Optional[str] = None) -> str:
    """
    Format a datetime object according to the time_format setting.

    Args:
        dt: datetime object to format
        time_format: Optional override (defaults to settings.time_format)

    Returns:
        Formatted time string (12h: "3:45 PM" or 24h: "15:45")
    """
    if time_format is None:
        time_format = settings.time_format

    if time_format == "24h":
        return dt.strftime("%H:%M")
    else:  # Default to 12h
        return dt.strftime("%I:%M %p").lstrip(
            "0"
        )  # Remove leading zero, e.g., "3:45 PM" instead of "03:45 PM"


def current_datetime() -> datetime:
    """Return the current datetime in the configured timezone."""
    try:
        return datetime.now(ZoneInfo(settings.timezone))
    except ZoneInfoNotFoundError:
        return datetime.now(ZoneInfo("UTC"))


def current_date():
    """Return today's date in the configured timezone."""
    return current_datetime().date()


def format_print_datetime(
    dt: Optional[datetime] = None,
    time_format: Optional[str] = None,
    date_format: str = "%A, %B %d, %Y",
) -> str:
    """
    Format a timestamp for receipt headers.

    Returns strings like "Tuesday, April 21, 2026 3:45 PM" or
    "Tuesday, April 21, 2026 15:45", depending on the time_format setting.
    """
    dt = dt or current_datetime()
    return f"{dt.strftime(date_format)} {format_time(dt, time_format)}"


# Global settings instance
settings = load_config()
