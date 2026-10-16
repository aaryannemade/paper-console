# Agent Instructions

Use a Unix-like shell for local development commands. On Windows, use WSL.

## Project Context

Use `readme.md` as canonical project context for setup, architecture, and expected behavior.

Rules:
- Check `readme.md` before making architecture, workflow, or UX assumptions.
- If instructions conflict, prefer: explicit user chat instructions, then `AGENTS.md`, then `readme.md`.
- Do not copy large sections of `readme.md` into responses unless explicitly requested; summarize relevant parts.

## Print Outputs

`PC1_PRINTER_OUTPUT` selects where receipts go: `esp32` (production default), `console` (default for `just backend` and tests) or `usb` (development only). All outputs share `app/drivers/printer_raster.py`, so a console/PNG capture is the exact bitmap a printer receives. To capture receipts from a live server, set `PC1_PRINT_CAPTURE_DIR`. Keep CSN-A2 byte encoding in `app/drivers/escpos.py`, not in an output. To exercise the production `esp32` output locally, run `PC1_PRINTER_OUTPUT=esp32 just backend` with `just relay-sim` (`testing/relay_simulator.py`, the reference device implementation of `docs/relay-protocol.md`); keep the simulator in step with protocol changes.

## Console Raster Preview for Print Debugging

When validating printer output (layout, clipping, alignment, density, or black/white dot rendering), run this feedback loop:

```bash
./.venv/bin/python testing/render_all_prints.py --module-type astronomy --output testing/artifacts/debug/feedback_full_print.png
./.venv/bin/python testing/console_raster_preview.py --image testing/artifacts/debug/feedback_full_print.png --dots-width 384 --cols 96

./.venv/bin/python testing/render_all_prints.py --channel 3 --output testing/artifacts/debug/feedback_full_print.png
./.venv/bin/python testing/console_raster_preview.py --image testing/artifacts/debug/feedback_full_print.png --dots-width 384 --cols 96
```

This workflow:
- Renders the full receipt bitmap through the real print pipeline (`testing/render_all_prints.py`)
- Saves PNG output under `testing/artifacts/debug/`
- Prints a console dot-map preview from that exact full receipt image

Use this fallback command only for synthetic calibration patterns:

```bash
./.venv/bin/python testing/console_raster_preview.py --pattern targets --dots-width 384 --cols 96
```

Rules:
- Prefer `./.venv/bin/python` in this repo (do not assume `python` is on `PATH`).
- Prefer full-print feedback loop previews over synthetic patterns when evaluating actual print changes.
- Use `--dots-width 384` by default for the 58mm printer path unless a different width is explicitly provided.
- Use built-in patterns (`grid`, `checker`, `bars`, `targets`) for deterministic diagnostics.
- Use `--image <path>` to inspect an actual bitmap that would be printed.
- Share/paste generated dot-map output in agent responses when requesting print QA feedback.

## Full Print Snapshot Gallery

Use `testing/render_all_prints.py` as the unified debug renderer.

Full sweep (complete up-to-date PNG gallery):

```bash
./.venv/bin/python testing/render_all_prints.py
```

Optional output directory:

```bash
./.venv/bin/python testing/render_all_prints.py --output-dir testing/artifacts/print
```

Targeted single renders:

```bash
./.venv/bin/python testing/render_all_prints.py --channel 3
./.venv/bin/python testing/render_all_prints.py --module-id default-weather-001
./.venv/bin/python testing/render_all_prints.py --module-type astronomy
```

Rules:
- This command is intended to refresh a gallery folder on every run.
- It overwrites snapshot content by clearing prior `*.png` files in the target folder and regenerating all outputs.
- It generates channel prints and module prints. (Setup/first-boot/system-ready receipts were removed with WiFi provisioning.)
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
./.venv/bin/python testing/render_settings_ui.py
./.venv/bin/python testing/render_settings_ui.py --reuse-servers
./.venv/bin/python testing/render_settings_ui.py --install-browser
```

Rules:
- This workflow refreshes the target screenshot folder each run.
- It captures the main settings tabs/modals and module editor screens.
- It seeds temporary modules (`UI TEST :: ...`) so every module-type editor is covered, then cleans them up.
- It writes run artifacts (`manifest.txt`, `failures.txt`, `notes.txt`) in the output folder.
- If Playwright reports missing Linux libs, use `npx --yes -p playwright playwright install-deps chromium` (requires sudo).
