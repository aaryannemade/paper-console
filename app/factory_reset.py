"""Application-data reset; never changes host networking, credentials or power."""

import logging
import app.config as config_module

logger = logging.getLogger(__name__)


def perform_factory_reset() -> dict:
    """Persist defaults and remove the old configuration backup.

    Preserve the server password so a physical reset cannot change authentication.
    Callers must publish config_module.settings to their runtime after success.
    """
    errors = []
    defaults = config_module.Settings()
    try:
        config_module.save_config(defaults)
    except Exception as exc:
        logger.exception("Application settings reset failed")
        return {"config_cleared": False, "errors": [str(exc)]}

    config_module.settings = defaults
    base_dir = config_module.get_config_dir()
    for name in ("config.json.bak",):
        try:
            (base_dir / name).unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"Failed to remove {name}: {exc}")
            logger.warning("Could not remove reset data %s", name, exc_info=True)

    return {"config_cleared": True, "errors": errors}
