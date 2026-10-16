"""Writable application state shared by local development and the container.

In the container PC1_DATA_DIR=/data is a bind mount. When unset, the existing
local-development paths remain in the project tree for backwards compatibility.
Bundled datasets, fonts and static UI assets stay with the application code.
"""

import os
from pathlib import Path

DATA_DIR_ENV = "PC1_DATA_DIR"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def data_dir(default: Path | None = None) -> Path:
    configured = os.environ.get(DATA_DIR_ENV, "").strip()
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise ValueError(f"{DATA_DIR_ENV} must be an absolute path")
        return path
    return default if default is not None else PROJECT_ROOT


def prepare_data_dir() -> Path:
    """Create the mounted directory and fail early if it is not writable."""
    path = data_dir()
    if os.environ.get(DATA_DIR_ENV, "").strip():
        path.mkdir(parents=True, exist_ok=True)
        if not os.access(path, os.W_OK | os.X_OK):
            raise RuntimeError(f"{DATA_DIR_ENV} directory is not writable: {path}")
    return path
