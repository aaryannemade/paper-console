#!/usr/bin/env python3
"""Build GitHub release notes for a version (used by .github/workflows/release.yml).

  python scripts/release_notes.py --check 0.1.0-beta.1
  python scripts/release_notes.py 0.1.0-beta.1 --image ghcr.io/owner/repo \
      --repo owner/repo [--previous-tag v0.0.9] [--latest] > notes.md

The version's section in CHANGELOG.md is required; release notes add the
container and firmware install instructions and, when a previous tag exists,
the commits since it.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?$")
FIRMWARE_PREFIX = "paper-console-relay"
BOARD = "xiao_esp32s3"


def changelog_section(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)",
        text,
        re.S | re.M,
    )
    if not match or not match.group(1).strip():
        raise SystemExit(f"CHANGELOG.md has no (non-empty) section '## [{version}]'")
    return match.group(1).strip()


def commits_since(previous_tag: str) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--no-merges", "--format=- %s (%h)", f"{previous_tag}..HEAD"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def firmware_names(version: str) -> tuple[str, str]:
    base = f"{FIRMWARE_PREFIX}-{version}-{BOARD}"
    return f"{base}-factory.bin", f"{base}-update.bin"


def build_notes(version, image, repo, previous_tag, latest):
    factory, update = firmware_names(version)
    prerelease = "-" in version
    tags = f"`{image}:{version}`" + (f" and `{image}:latest`" if latest else "")
    lines = []
    if prerelease:
        lines += [
            "> [!NOTE]",
            "> Pre-release: tested on the reference hardware, but expect rough edges.",
            "",
        ]
    lines += [
        changelog_section(version),
        "",
        "## Install",
        "",
        "### Container",
        "",
        f"Multi-arch image (`linux/amd64`, `linux/arm64`): {tags}.",
        "",
        "```bash",
        f"docker pull {image}:{version}",
        "```",
        "",
        "Portainer: create a stack from the attached `portainer-compose.yaml` "
        "(it defaults to `:latest`; set `PC1_IMAGE` to pin "
        f"`{image}:{version}`), replace `/path/to/data` with a host directory, "
        "and set `PC1_RELAY_TOKEN`, `PUID`/`PGID` and optionally "
        "`PC1_DEVICE_PASSWORD` in the stack environment. Settings live in the "
        "`/data` bind mount and survive image updates.",
        "",
        "### Relay firmware (Seeed XIAO ESP32S3)",
        "",
        "| File | Use |",
        "| --- | --- |",
        f"| `{update}` | **Update** an installed relay. Written at `0x10000`; keeps its WiFi/server/token settings. |",
        f"| `{factory}` | **New or erased board.** Bootloader, partitions and app merged for `0x0`; erases saved settings. |",
        "",
        "```bash",
        "# esptool 4.x or 5.x (pip install esptool); hold BOOT while plugging in if the port does not respond",
        f"esptool.py --chip esp32s3 --port /dev/ttyACM0 write_flash 0x10000 {update}",
        f"esptool.py --chip esp32s3 --port /dev/ttyACM0 write_flash 0x0 {factory}",
        "```",
        "",
        "From a checkout, `just fw-flash` inside `nix develop` builds and flashes the same "
        "firmware while keeping settings. After a factory flash, join the "
        "`PaperConsole-XXXXXX` WiFi network and enter your WiFi, the server URL and "
        "the relay token. The relay reports this release's version as `firmware` in `/health`.",
        "",
        "Verify downloads with `sha256sum -c SHA256SUMS`.",
        "",
    ]
    if previous_tag:
        commits = commits_since(previous_tag)
        lines += ["## Commits", ""]
        lines += commits if commits else ["- No commits."]
        lines += [
            "",
            f"**Full changelog:** https://github.com/{repo}/compare/{previous_tag}...v{version}",
        ]
    else:
        lines += [
            "First release of this fork; see the changes above and "
            f"[CHANGELOG.md](https://github.com/{repo}/blob/v{version}/CHANGELOG.md).",
        ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="Version without the leading v, e.g. 0.1.0-beta.1")
    parser.add_argument("--check", action="store_true", help="Only validate the version and changelog")
    parser.add_argument("--image", default="ghcr.io/OWNER/REPO")
    parser.add_argument("--repo", default="OWNER/REPO")
    parser.add_argument("--previous-tag", default="")
    parser.add_argument("--latest", action="store_true", help="Image is also tagged :latest")
    args = parser.parse_args()

    if not VERSION_RE.match(args.version):
        raise SystemExit(f"'{args.version}' is not a SemVer version like 1.2.3 or 1.2.3-beta.1")
    if args.check:
        changelog_section(args.version)
        print(f"ok: {args.version}")
        return
    sys.stdout.write(build_notes(args.version, args.image, args.repo, args.previous_tag, args.latest))


if __name__ == "__main__":
    main()
