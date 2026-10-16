set shell := ["bash", "-eu", "-o", "pipefail", "-c"]
set positional-arguments

# List available development commands.
default:
    @just --list

# Start the API; optionally pass a bind address and port.
# Prints go to the console log unless PC1_PRINTER_OUTPUT is set (e.g. usb).
backend host="127.0.0.1" port="8000":
    PC1_PRINTER_OUTPUT="${PC1_PRINTER_OUTPUT:-console}" PYTHONUNBUFFERED=1 \
        python -m uvicorn app.main:app --host "$1" --port "$2" --reload --reload-dir app

# Send an ESP32-style input event, e.g. `just device position 5`, `just device press`.
device *args:
    python testing/device_event.py "$@"

# Run the ESP32 relay simulator (use with PC1_PRINTER_OUTPUT=esp32); e.g. `just relay-sim --baud 0`.
relay-sim *args:
    PYTHONUNBUFFERED=1 python testing/relay_simulator.py "$@"

# Install the locked frontend dependencies (first run or lockfile changes).
frontend-install:
    npm --prefix web ci

# Start the settings UI; accepts additional Vite arguments.
frontend *args:
    npm --prefix web run dev -- "$@"

# Build the settings UI into web/dist so the backend can serve it on its own port.
frontend-build:
    npm --prefix web run build

# Run the backend tests; accepts additional pytest arguments.
test *args:
    python -m pytest -q testing "$@"

# Validate every Python module's icon references against the bundled icons.
icons:
    python scripts/validate_icons.py
