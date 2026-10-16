"""Test-wide defaults, applied before any app module is imported."""

import os

# The production default is the ESP32 relay, which is unreachable in tests.
# Tests that exercise other outputs construct them explicitly.
os.environ["PC1_PRINTER_OUTPUT"] = "console"
os.environ.pop("PC1_PRINT_CAPTURE_DIR", None)
