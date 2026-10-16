"""Version from the container image or the current development checkout."""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def read_version(project_root: Path | None = None) -> str:
    root = project_root or Path(__file__).resolve().parents[1]
    try:
        version = (root / ".version").read_text(encoding="utf-8").strip()
        if version:
            return version
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        logger.debug("git version lookup failed", exc_info=True)
    return "unknown"
