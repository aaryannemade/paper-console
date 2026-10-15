set shell := ["bash", "-eu", "-o", "pipefail", "-c"]
set positional-arguments

# List available development commands.
default:
    @just --list

# Start the API; optionally pass a bind address and port.
backend host="127.0.0.1" port="8000":
    python -m uvicorn app.main:app --host "$1" --port "$2" --reload --reload-dir app

# Install the locked frontend dependencies (first run or lockfile changes).
frontend-install:
    npm --prefix web ci

# Start the settings UI; accepts additional Vite arguments.
frontend *args:
    npm --prefix web run dev -- "$@"

# Run the backend tests; accepts additional pytest arguments.
test *args:
    python -m pytest -q testing "$@"
