# Testing and previews

Run these commands inside `nix develop` (or prefix them with `nix develop --command`):

```bash
just test
just icons
python testing/render_all_prints.py             # Full gallery; replaces old PNGs
python testing/render_all_prints.py --channel 3 --output testing/artifacts/debug/channel.png
python testing/render_all_prints.py --module-type astronomy
python testing/console_raster_preview.py --image testing/artifacts/debug/channel.png --dots-width 384 --cols 96
python testing/render_settings_ui.py            # UI screenshot gallery (Playwright)
```

The test suite uses the console print output; no printer or ESP32 is required.
The full print sweep also renders unassigned module types and writes
`failures.txt`. The UI sweep seeds temporary modules for every editor and cleans
them up; it writes `manifest.txt`, `failures.txt`, and `notes.txt` under
`testing/artifacts/ui/`. Install Playwright Chromium on first use with
`python testing/render_settings_ui.py --install-browser`.

For specific output paths and printer debugging steps see `AGENTS.md`.
