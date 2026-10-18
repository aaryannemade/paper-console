# PlatformIO extra script: firmware version and the merged factory image.
#
# Version (PC1_FIRMWARE_VERSION, reported in heartbeats and /health): the
# PC1_FIRMWARE_VERSION environment variable (set by the Release workflow), else
# `git describe` of this checkout, else "dev".
#
# `pio run -t factory` additionally writes .pio/build/<env>/factory.bin: the
# bootloader, partition table, boot_app0 and app merged at their real offsets
# (taken from this build, not hard-coded), for flashing at 0x0 on a blank or
# erased board. Writing it erases the saved WiFi/server/token settings (NVS lies
# between the partition table and the app); `firmware.bin` at the app offset
# updates without touching them.
import os
import subprocess

Import("env")  # noqa: F821 - provided by PlatformIO


def _version():
    explicit = os.environ.get("PC1_FIRMWARE_VERSION", "").strip()
    if explicit:
        return explicit
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=env.subst("$PROJECT_DIR"),  # noqa: F821
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        if out:
            return out.lstrip("v")
    except Exception:
        pass
    return "dev"


version = _version()[:48]
env.Append(CPPDEFINES=[("PC1_FIRMWARE_VERSION", env.StringifyMacro(version))])  # noqa: F821
print(f"Paper Console firmware version: {version}")


def _merge_factory(target, source, env):
    build_dir = env.subst("$BUILD_DIR")
    images = [(offset, env.subst(path)) for offset, path in env.get("FLASH_EXTRA_IMAGES", [])]
    images.append((env.subst("$ESP32_APP_OFFSET"), os.path.join(build_dir, "firmware.bin")))
    images.sort(key=lambda item: int(item[0], 16))
    esptool = os.path.join(env.PioPlatform().get_package_dir("tool-esptoolpy"), "esptool.py")
    output = os.path.join(build_dir, "factory.bin")
    cmd = [
        env.subst("$PYTHONEXE"), esptool, "--chip", env.BoardConfig().get("build.mcu"),
        "merge_bin", "-o", output,
        # Keep the flash mode/frequency/size already written into the bootloader.
        "--flash_mode", "keep", "--flash_freq", "keep", "--flash_size", "keep",
    ]
    for offset, path in images:
        cmd += [offset, path]
    print("Merging factory image:", ", ".join(f"{o} {os.path.basename(p)}" for o, p in images))
    if subprocess.call(cmd) != 0:
        env.Exit(1)


env.AddCustomTarget(  # noqa: F821
    name="factory",
    dependencies="$BUILD_DIR/firmware.bin",
    actions=[_merge_factory],
    title="Factory image",
    description="Merge bootloader, partitions and app into factory.bin",
)
