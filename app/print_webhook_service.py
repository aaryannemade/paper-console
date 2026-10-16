from dataclasses import dataclass
import re
import secrets
import uuid
from typing import Any, Dict, List, Mapping, Optional

from fastapi import HTTPException, Request

from app.config import ChannelConfig, ModuleInstance, PrintWebhookConfig
from app.modules import print_webhook


@dataclass
class PreparedPrintWebhookJob:
    module_id: str
    job: Dict[str, Any]


@dataclass
class ResolvedPrintWebhookTarget:
    module_id: str
    config: PrintWebhookConfig
    module_name: str
    metadata_lines: List[str]


def slugify_endpoint(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", (value or "").strip().lower())
    slug = slug.strip("-")
    return slug or f"hook-{uuid.uuid4().hex[:8]}"


def generate_token() -> str:
    return secrets.token_urlsafe(18)


def _endpoint_in_use(
    modules: Mapping[str, ModuleInstance],
    endpoint_path: str,
    *,
    exclude_module_id: Optional[str] = None,
) -> bool:
    normalized_path = str(endpoint_path or "").strip().strip("/")
    if not normalized_path:
        return False

    for existing_id, existing in modules.items():
        if existing_id == exclude_module_id or existing.type != "print_webhook":
            continue
        existing_path = str((existing.config or {}).get("endpoint_path") or "").strip().strip("/")
        if existing_path == normalized_path:
            return True
    return False


def _generate_unique_endpoint_path(
    modules: Mapping[str, ModuleInstance],
    seed: str,
    *,
    exclude_module_id: Optional[str] = None,
) -> str:
    base = slugify_endpoint(seed)
    candidate = base
    suffix = 2

    while _endpoint_in_use(
        modules,
        candidate,
        exclude_module_id=exclude_module_id,
    ):
        candidate = f"{base}-{suffix}"
        suffix += 1

    return candidate


def normalize_module_config(
    modules: Mapping[str, ModuleInstance],
    module: ModuleInstance,
    *,
    generate_token_if_missing: bool = False,
) -> None:
    if module.type != "print_webhook":
        return

    config = module.config if isinstance(module.config, dict) else {}

    if not config.get("endpoint_path"):
        seed = module.name or module.id or "print-webhook"
        config["endpoint_path"] = _generate_unique_endpoint_path(
            modules,
            seed,
            exclude_module_id=module.id,
        )
    else:
        config["endpoint_path"] = slugify_endpoint(str(config["endpoint_path"]))

    for key in ("accept_text", "accept_images", "accept_json", "print_when_channel_active"):
        if key not in config:
            config[key] = True

    try:
        max_height = int(config.get("max_image_height_dots", 4096))
    except Exception:  # noqa: BLE001
        max_height = 4096
    config["max_image_height_dots"] = min(8192, max(64, max_height))
    config["token"] = str(config.get("token") or "").strip()
    if generate_token_if_missing and not config["token"]:
        config["token"] = generate_token()

    module.config = config


def validate_endpoint_uniqueness(
    modules: Mapping[str, ModuleInstance],
    module_id: str,
    module: ModuleInstance,
) -> None:
    if module.type != "print_webhook":
        return

    endpoint_path = str((module.config or {}).get("endpoint_path") or "").strip().strip("/")
    if not endpoint_path:
        raise HTTPException(status_code=400, detail="Endpoint path is required")

    if _endpoint_in_use(modules, endpoint_path, exclude_module_id=module_id):
        raise HTTPException(
            status_code=400,
            detail=f"Endpoint path '{endpoint_path}' is already in use",
        )


def build_metadata_lines(
    request: Request,
    config: PrintWebhookConfig,
) -> List[str]:
    lines: List[str] = []

    client_host = getattr(request.client, "host", None)
    if config.print_sender_ip and client_host:
        lines.append(f"From: {client_host}")

    if config.print_content_type:
        content_type = print_webhook.normalize_content_type(
            request.headers.get("content-type", "")
        )
        if content_type:
            lines.append(f"Type: {content_type}")

    if config.print_user_agent:
        user_agent = (request.headers.get("user-agent") or "").strip()
        if user_agent:
            lines.append(f"UA: {user_agent}")

    return lines


async def read_limited_request_body(
    request: Request,
    *,
    max_bytes: Optional[int] = None,
) -> bytes:
    if max_bytes is None:
        max_bytes = print_webhook.INCOMING_WEBHOOK_MAX_REQUEST_BYTES

    error_detail = (
        "Request body too large. "
        f"Max {max_bytes / (1024 * 1024):g} MB."
    )
    content_length = (request.headers.get("content-length") or "").strip()
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail=error_detail)
        except ValueError:
            pass

    chunks: List[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail=error_detail)
        chunks.append(chunk)

    return b"".join(chunks)


def resolve_incoming_target(
    *,
    modules: Mapping[str, ModuleInstance],
    channels: Mapping[int, ChannelConfig],
    endpoint_path: str,
    request: Request,
    dial_position: Optional[int],
) -> ResolvedPrintWebhookTarget:
    module = _find_module_by_path(modules, endpoint_path)
    if not module:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")

    config = PrintWebhookConfig(**(module.config or {}))
    bearer_token = _extract_bearer_token(request)
    if config.token and bearer_token != config.token:
        raise HTTPException(status_code=401, detail="Invalid bearer token")

    if config.print_when_channel_active and not _module_is_assigned_to_current_channel(channels, dial_position, module.id):
        raise HTTPException(
            status_code=503,
            detail="Print webhook module is not on the active channel",
        )

    return ResolvedPrintWebhookTarget(
        module_id=module.id,
        config=config,
        module_name=module.name or "PRINT WEBHOOK",
        metadata_lines=build_metadata_lines(request, config),
    )


def finalize_incoming_job(
    *,
    target: ResolvedPrintWebhookTarget,
    content_type: str,
    body: bytes,
) -> PreparedPrintWebhookJob:
    try:
        job = print_webhook.parse_request_payload(
            content_type=content_type,
            body=body,
            config=target.config,
            module_name=target.module_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if target.metadata_lines:
        job["metadata_lines"] = target.metadata_lines

    return PreparedPrintWebhookJob(module_id=target.module_id, job=job)


def print_job(
    *,
    modules: Mapping[str, ModuleInstance],
    printer,
    max_print_lines: int,
    module_id: str,
    job: Dict[str, Any],
) -> None:
    module = modules.get(module_id)
    if not module or module.type != "print_webhook":
        return

    config = PrintWebhookConfig(**(module.config or {}))
    module_name = module.name or "PRINT WEBHOOK"

    if hasattr(printer, "blip"):
        printer.blip()

    if hasattr(printer, "reset_buffer"):
        printer.reset_buffer(max_print_lines)

    print_webhook.print_parsed_job(printer, job, config, module_name)

    if hasattr(printer, "flush_buffer"):
        printer.flush_buffer()


def _find_module_by_path(
    modules: Mapping[str, ModuleInstance],
    endpoint_path: str,
) -> Optional[ModuleInstance]:
    normalized_path = endpoint_path.strip().strip("/")
    for module in modules.values():
        if module.type != "print_webhook":
            continue
        config = module.config or {}
        candidate = str(config.get("endpoint_path") or "").strip().strip("/")
        if candidate == normalized_path:
            return module
    return None


def _extract_bearer_token(request: Request) -> str:
    auth_header = (request.headers.get("authorization") or "").strip()
    if not auth_header.lower().startswith("bearer "):
        return ""
    return auth_header[7:].strip()


def _module_is_assigned_to_current_channel(
    channels: Mapping[int, ChannelConfig],
    dial_position: Optional[int],
    module_id: str,
) -> bool:
    if dial_position is None:
        return False

    channel = channels.get(dial_position)
    if not channel or not channel.modules:
        return False

    return any(assignment.module_id == module_id for assignment in channel.modules)
