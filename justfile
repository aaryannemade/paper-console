set shell := ["bash", "-eu", "-o", "pipefail", "-c"]
set positional-arguments

# List available development commands.
default:
    @just --list

# Development-only credentials; exported shell values take precedence.
dev_password := "123456789"
dev_relay_token := "12345678901234567890"

# Prints go to the console log unless PC1_PRINTER_OUTPUT is set (e.g. usb).
# Login password defaults to dev_password, so no .device_password is generated.
# Start the API; optionally pass a bind address and port.
backend host="127.0.0.1" port="8000":
    PC1_PRINTER_OUTPUT="${PC1_PRINTER_OUTPUT:-console}" \
    PC1_DEVICE_PASSWORD="${PC1_DEVICE_PASSWORD:-{{dev_password}}}" \
    PC1_RELAY_TOKEN="${PC1_RELAY_TOKEN:-{{dev_relay_token}}}" PYTHONUNBUFFERED=1 \
        python -m uvicorn app.main:app --host "$1" --port "$2" --reload --reload-dir app

# Backend + settings UI, receipts logged to the console; `--bind 0.0.0.0` for LAN/SSH.
dev-console *args:
    PC1_PRINTER_OUTPUT=console "{{just_executable()}}" _dev 127.0.0.1 "$@"

# Backend + settings UI, printing to a USB printer; `--bind 0.0.0.0` for LAN/SSH.
dev-usb *args:
    PC1_PRINTER_OUTPUT=usb "{{just_executable()}}" _dev 127.0.0.1 "$@"

# Backend + settings UI on the LAN (0.0.0.0), printing through the ESP32 relay.
dev-esp32 *args:
    PC1_PRINTER_OUTPUT=esp32 "{{just_executable()}}" _dev 0.0.0.0 "$@"

# Run backend and frontend in their own process groups; stop both when either exits.
# Both listen on the bind address (default per recipe; override with --bind ADDR).
# Backend output is also written to logs/dev-<output>.log (recreated on each start).
[private]
_dev default_host *args:
    #!/usr/bin/env bash
    set -euo pipefail
    set -m
    host="$1"; shift
    while (($#)); do
        case "$1" in
            --bind) [[ $# -ge 2 && -n "$2" ]] || { echo "dev: --bind needs an address" >&2; exit 2; }
                    host="$2"; shift 2 ;;
            --bind=?*) host="${1#--bind=}"; shift ;;
            *) echo "dev: unknown option '$1' (supported: --bind ADDR, e.g. --bind 0.0.0.0)" >&2; exit 2 ;;
        esac
    done
    set -- "$host"
    log_dir="{{justfile_directory()}}/logs"
    log_file="$log_dir/dev-$PC1_PRINTER_OUTPUT.log"
    mkdir -p "$log_dir"
    : > "$log_file"
    pids=()
    cleanup() {
        for pid in "${pids[@]}"; do kill -TERM -- "-$pid" 2>/dev/null || true; done
        wait 2>/dev/null || true
    }
    trap cleanup EXIT
    trap 'exit 130' INT TERM
    echo "dev: output=$PC1_PRINTER_OUTPUT backend=http://$1:8000 ui=http://$1:5173"
    if [[ "$1" != 127.* && "$1" != localhost && "$1" != ::1 ]]; then
        echo "dev: listening beyond loopback; open http://<this-host-ip>:5173 from other devices"
    fi
    echo "dev: backend log: tail -f logs/dev-$PC1_PRINTER_OUTPUT.log"
    # Subshell so the pipeline is one process group that cleanup can stop.
    ( "{{just_executable()}}" backend "$1" 8000 </dev/null 2>&1 | tee -a "$log_file" ) & pids+=($!)
    # Wildcard binds are reachable via localhost; a specific address is not.
    case "$1" in
        0.0.0.0|::|127.*|localhost|::1) backend_url="http://localhost:8000" ;;
        *:*) backend_url="http://[$1]:8000" ;;
        *) backend_url="http://$1:8000" ;;
    esac
    PC1_BACKEND_URL="$backend_url" "{{just_executable()}}" frontend --host "$1" </dev/null & pids+=($!)
    wait -n

# Send an ESP32-style input event, e.g. `just device position 5`, `just device press`.
device *args:
    PC1_RELAY_TOKEN="${PC1_RELAY_TOKEN:-{{dev_relay_token}}}" python testing/device_event.py "$@"

# Run the ESP32 relay simulator (use with `just dev-esp32`); e.g. `just relay-sim --baud 0`.
relay-sim *args:
    PC1_RELAY_TOKEN="${PC1_RELAY_TOKEN:-{{dev_relay_token}}}" PYTHONUNBUFFERED=1 \
        python testing/relay_simulator.py "$@"

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

# Absolute path: `pio device monitor` filters resolve a relative -d twice.
firmware_dir := justfile_directory() / "firmware"

# Build the ESP32-S3 relay firmware (firmware/, PlatformIO).
fw-build:
    pio run -d "{{firmware_dir}}"

# Build and flash the relay firmware; port defaults to /dev/ttyACM0 (PC1_ESP32_PORT).
fw-flash port="":
    pio run -d "{{firmware_dir}}" -t upload --upload-port "${1:-${PC1_ESP32_PORT:-/dev/ttyACM0}}"

# Show the relay's USB log (Ctrl+C to quit).
fw-monitor port="":
    pio device monitor -d "{{firmware_dir}}" -p "${1:-${PC1_ESP32_PORT:-/dev/ttyACM0}}"

# Erase the whole ESP32 flash, including saved WiFi/server/token settings.
fw-erase port="":
    esptool --port "${1:-${PC1_ESP32_PORT:-/dev/ttyACM0}}" erase-flash
