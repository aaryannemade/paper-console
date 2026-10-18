# Agent Instructions

Use a Unix-like shell for local development commands. On Windows, use WSL.

## Project Context

Use `readme.md` as canonical project context for setup, architecture, and expected behavior.

Rules:
- Check `readme.md` before making architecture, workflow, or UX assumptions.
- If instructions conflict, prefer: explicit user chat instructions, then `AGENTS.md`, then `readme.md`.
- Do not copy large sections of `readme.md` into responses unless explicitly requested; summarize relevant parts.

## Print Outputs

`PC1_PRINTER_OUTPUT` selects where receipts go: `esp32` (production default), `console` (default for `just backend` and tests) or `usb` (development only). `usb` prefers the printer's native USB printer-class bulk endpoint (`app/drivers/usb_bulk_printer.py`, PyUSB/libusb); `PC1_PRINTER_PORT` explicitly selects a USB-to-serial adapter. All outputs share `app/drivers/printer_raster.py`, so a console/PNG capture is the exact bitmap a printer receives. To capture receipts from a live server, set `PC1_PRINT_CAPTURE_DIR`. Keep CSN-A2 byte encoding in `app/drivers/escpos.py`, not in an output. `just dev-console`, `just dev-usb` and `just dev-esp32` start the backend (port 8000) and Vite UI (port 5173) together with that output (`--bind ADDR` overrides the listen address, default `127.0.0.1`, or `0.0.0.0` for `dev-esp32`); `just backend` and the `dev-*` recipes default to the development-only login password `123456789` and relay token `12345678901234567890`, overridable by exported `PC1_DEVICE_PASSWORD`/`PC1_RELAY_TOKEN`. To exercise the production `esp32` output locally, run `just dev-esp32` (or `PC1_PRINTER_OUTPUT=esp32 just backend`) with `just relay-sim` (`testing/relay_simulator.py`, the reference device implementation of the "Relay Protocol" section in `readme.md`); keep the simulator in step with protocol changes.

The real device side is the PlatformIO firmware in `firmware/` (XIAO ESP32S3): `just fw-build`, `just fw-flash`, `just fw-monitor` inside `nix develop`. Keep it, the simulator and that README section consistent.

## Container Runtime

### Updating the development and container toolchains

- Check current upstream releases before choosing versions: Node's release index
  (`https://nodejs.org/dist/index.json`), `npm view npm version`, and PyPI's pip
  metadata (`https://pypi.org/pypi/pip/json`). Check Python release availability
  and the corresponding official Docker image tags as well.
- Run `nix flake update nixpkgs` to refresh the locked `nixpkgs-unstable` snapshot.
  A fresh snapshot can still lag upstream. Inspect the actual Python, Node, npm
  and pip versions with `nix develop --command ...`; do not assume bundled npm
  or Nix-packaged pip is the latest.
- `flake.nix` currently overrides npm and pip to match newer upstream releases.
  When updating these overrides, update the version, source URL and verified
  source hash together. Remove an override when the locked package set provides
  the desired version. Keep pins reproducible; do not install floating `latest`
  packages in the shell hook.
- Keep `Dockerfile` aligned with the validated Python/Node versions and the
  explicit npm/pip versions. Update both frontend and runtime base images; use
  `--pull` when testing. Python requirements are installed from PyPI in Docker,
  whereas the Nix shell uses its locked package set, so test both environments.
- Validate with `nix develop --command just test`, then `npm ci --prefix web`,
  `npm run lint --prefix web`, `npm run build --prefix web` and `just fw-build`
  inside the updated shell. Review npm install-script policy changes when
  upgrading npm majors. Run `nix flake check --no-build --all-systems` to check
  declared platform support; report upstream platform blockers explicitly.
- Build the container and run the backend tests against its installed runtime
  dependencies (install `requirements-dev.txt` only in a disposable test
  container). Run `python -m pip check`. Smoke-test the production image as its
  non-root user with a read-only root, temporary `/tmp`, writable `/data`, and
  `PC1_PRINTER_OUTPUT=console`: check `/api/health`, the UI, and an authenticated
  print request, waiting for the receipt capture to be written. Use temporary
  test data and clean up test containers afterward.
- This host uses Podman through the `docker` command. Supply
  `--build-arg BUILDPLATFORM=linux/amd64` for native builds if needed; use
  `--format docker` with Podman when checking the image's HEALTHCHECK metadata.
  A rootless bind mount must be writable by the container UID (for example,
  `podman unshare chown 1000:1000 <temporary-test-directory>`).
- Update `readme.md` with validated versions and any compatibility exceptions.
  As of October 2026, Python 3.15.0's Nix closure failed in tkinter tests, so
  Python 3.14.7 is used in both environments. Current nixpkgs dropped Intel
  macOS; Apple Silicon evaluation is blocked by the Slack build dependency
  `jaraco-path` being marked broken. Recheck these upstream issues on the next
  update rather than silently bypassing failures or claiming untested support.

The multi-stage `Dockerfile` serves the built UI and API as UID/GID 1000:1000 with `PC1_DATA_DIR=/data`. `docker-compose.yaml` and `portainer-compose.yaml` both use an absolute host bind mount (`/path/to/data:/data`); replace the example host path before deployment. Both run as `user: "${PUID:-1000}:${PGID:-1000}"`; PUID/PGID must match the owner of the data directory. Their root filesystem is read-only with a temporary `/tmp`. Settings, generated login password and Adventure saves belong under `/data`; bundled datasets, icons and fonts remain read-only in the image. Restoring defaults means stopping the stack and clearing the dedicated host bind-mount directory; `down -v` does not remove bind-mounted files. The container runs one Uvicorn worker because scheduling and printer reservations are process-local. Container printing defaults to `esp32`; use `console` for smoke tests without firmware.

## Console Raster Preview for Print Debugging

When validating printer output (layout, clipping, alignment, density, or black/white dot rendering), run this feedback loop:

```bash
python testing/render_all_prints.py --module-type astronomy --output testing/artifacts/debug/feedback_full_print.png
python testing/console_raster_preview.py --image testing/artifacts/debug/feedback_full_print.png --dots-width 384 --cols 96

python testing/render_all_prints.py --channel 3 --output testing/artifacts/debug/feedback_full_print.png
python testing/console_raster_preview.py --image testing/artifacts/debug/feedback_full_print.png --dots-width 384 --cols 96
```

This workflow:
- Renders the full receipt bitmap through the real print pipeline (`testing/render_all_prints.py`)
- Saves PNG output under `testing/artifacts/debug/`
- Prints a console dot-map preview from that exact full receipt image

Use this fallback command only for synthetic calibration patterns:

```bash
python testing/console_raster_preview.py --pattern targets --dots-width 384 --cols 96
```

Rules:
- Run these commands inside `nix develop` (or prefix with `nix develop --command`).
- Prefer full-print feedback loop previews over synthetic patterns when evaluating actual print changes.
- Use `--dots-width 384` by default for the 58mm printer path unless a different width is explicitly provided.
- Use built-in patterns (`grid`, `checker`, `bars`, `targets`) for deterministic diagnostics.
- Use `--image <path>` to inspect an actual bitmap that would be printed.
- Share/paste generated dot-map output in agent responses when requesting print QA feedback.

## Full Print Snapshot Gallery

Use `testing/render_all_prints.py` as the unified debug renderer.

Full sweep (complete up-to-date PNG gallery):

```bash
python testing/render_all_prints.py
```

Optional output directory:

```bash
python testing/render_all_prints.py --output-dir testing/artifacts/print
```

Targeted single renders:

```bash
python testing/render_all_prints.py --channel 3
python testing/render_all_prints.py --module-id default-weather-001
python testing/render_all_prints.py --module-type astronomy
```

Rules:
- This command is intended to refresh a gallery folder on every run.
- It overwrites snapshot content by clearing prior `*.png` files in the target folder and regenerating all outputs.
- It generates channel prints and module prints.
- It also renders all registered module types (including unassigned ones). For richer online-module snapshots, define optional test env vars in `.env`:
  - `PC1_TEST_NEWS_API_KEY`
  - `PC1_TEST_EMAIL_USER`
  - `PC1_TEST_EMAIL_APP_PASSWORD`
  - `PC1_TEST_EMAIL_HOST` (optional, default depends on module)
  - `PC1_TEST_RSS_FEEDS` (comma-separated URLs)
  - `PC1_TEST_CALENDAR_ICS_URL`
  - `PC1_TEST_WEBHOOK_URL`
  - `PC1_TEST_WEBHOOK_METHOD`
  - `PC1_TEST_WEBHOOK_JSON_PATH`

## Settings UI Snapshot Gallery

Use `testing/render_settings_ui.py` as the unified settings UI visual regression helper.

```bash
python testing/render_settings_ui.py
python testing/render_settings_ui.py --reuse-servers
python testing/render_settings_ui.py --install-browser
```

Rules:
- This workflow refreshes the target screenshot folder each run.
- It captures the main settings tabs/modals and module editor screens.
- It seeds temporary modules (`UI TEST :: ...`) so every module-type editor is covered, then cleans them up.
- It writes run artifacts (`manifest.txt`, `failures.txt`, `notes.txt`) in the output folder.
- If Playwright reports missing Linux libs, use `npx --yes -p playwright playwright install-deps chromium` (requires sudo).

## Releases

Releases are cut only by the manual `Release` workflow (`.github/workflows/release.yml`) from `main`; `docker-image.yml` is a manual development build that must never publish `:latest` or version tags. Before a release, add a `## [<version>]` section to `CHANGELOG.md` (the workflow refuses versions without one; `scripts/release_notes.py --check <version>` validates locally). Firmware versions come from `PC1_FIRMWARE_VERSION` (set by the workflow) or `git describe` via `firmware/scripts/release.py`; do not hard-code them in `platformio.ini`. Validate workflow edits with `nix shell nixpkgs#actionlint nixpkgs#shellcheck --command actionlint`.
