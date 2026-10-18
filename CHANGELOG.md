# Changelog

All notable changes to this fork are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/). The Release workflow publishes the
section matching the released version as the GitHub release notes; add a
section before each release.

## [Unreleased]

## [0.1.0-beta.1] - 2026-10-18

First release of the container + ESP32-S3 architecture. The Raspberry Pi
appliance becomes a container (settings UI, API, rendering, scheduling) and a
Seeed XIAO ESP32S3 relay that drives the CSN-A2 thermal printer over TTL UART.
Hardware acceptance passed on the real relay, printer and production container.

### Added

- **Container image** (`ghcr.io/aaryannemade/paper-console`) for
  `linux/amd64` and `linux/arm64`: serves the UI and API on one configurable
  port, runs as a non-root user with a read-only root filesystem, keeps
  settings, the generated password and Adventure saves in a `/data` bind mount,
  and has an HTTP health check. Docker Compose and Portainer stack files.
- **ESP32-S3 relay firmware** (`firmware/`, PlatformIO): captive-portal WiFi
  setup and status page, heartbeats, pushed print jobs written to the printer
  UART at 9600 baud with cancellation between records, input events from an
  EC11 rotary encoder with push button, local test print.
- **HTTP relay protocol** between container and relay with a shared bearer
  token, idempotent events and chunks, backpressure, job status callbacks with
  polling fallback, and no replay of uncertain prints. A reference simulator
  (`just relay-sim`) and a command-line device (`just device ...`) for
  development without hardware.
- **OLED display** (SSD1306 128×64, optional): channel name or number while
  turning the knob, open menus with the option under the knob, printing,
  WiFi setup and disconnected states, and an idle clock (NTP plus the server's
  timezone and 12h/24h setting) with WiFi signal bars. Burn-in shift and
  dimming; boot-time I²C diagnostics including swapped SDA/SCL detection.
- **Channel names** (up to 24 characters), edited in the settings UI, shown on
  the OLED, the relay page and the channel list receipt.
- **Device health**: public `GET /health` on the relay (schema 1) and a health
  panel and status dot in the settings UI, with explicit stale/offline states.
- **Encoder reset sequence** (hold 10 s, clockwise, anticlockwise, click) and
  BOOT held 5 s to erase the relay's WiFi/server/token settings.
- Nix flake development shell and `just` recipes (`dev-console`, `dev-usb`,
  `dev-esp32`, `fw-build`, `fw-flash`, `fw-monitor`, `test`, ...); native USB
  printer output for development.

### Changed

- Receipts are sent to the printer as 64-row downloaded bitmap bands
  (`GS *` / `GS / 0`), fixing vertical stretching and pale dithering seen with
  streaming raster at 9600 baud.
- Quick Actions: the receipt no longer prints the dial position, and an unused
  menu closes silently after 2 minutes instead of printing a timeout receipt.
- Toolchain: Python 3.14.7, Node.js 26.12.0, npm 12.2.0, pip 26.2.1 in both the
  Nix shell and the container.
- Documentation consolidated into the README (relay protocol, printer UART,
  scheduling, known limitations, troubleshooting).

### Removed

- Raspberry Pi host administration: GPIO access, WiFi provisioning and access
  point, captive portal on the server, SSH/OS password, clock, reboot and update
  controls, setup/welcome receipts.

### Known limitations

- 9600-baud printing (~26 s for a 500-row receipt); faint seams in dithered
  areas between bands; cancellation takes up to ~3 s to reach a band boundary.
- Paper-out, power and busy are not sensed: "ready" and "completed" mean the
  bytes left the UART.
- No offline queue: prints while the relay is unreachable are skipped.
- One relay per network; the relay settings page has no login.

[Unreleased]: https://github.com/aaryannemade/paper-console/compare/v0.1.0-beta.1...HEAD
[0.1.0-beta.1]: https://github.com/aaryannemade/paper-console/releases/tag/v0.1.0-beta.1
