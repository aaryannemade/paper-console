{
  description = "Paper Console development environment (console, USB and ESP32 relay)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

  outputs = { nixpkgs, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python312.withPackages (ps: with ps; [
            # Keep in sync with requirements.txt and requirements-dev.txt.
            fastapi
            uvicorn
            pydantic
            requests
            feedparser
            icalendar
            tzdata
            python-dateutil
            astral
            beautifulsoup4
            croniter
            pyserial
            pyusb
            qrcode
            pillow # qrcode[pil] and receipt raster rendering
            jsonschema
            slack-bolt
            aiohttp
            pytest
            pytest-asyncio

            # uvicorn[standard] extras
            httptools
            python-dotenv
            pyyaml
            uvloop
            watchfiles
            websockets
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.nodejs_22 # Vite 7 requires Node >= 20.19 or >= 22.12.
              pkgs.git
              pkgs.just
              pkgs.bash
              pkgs.lsof
              pkgs.libusb1 # Native USB printer-class transport (development)

              # ESP32-S3 relay firmware (firmware/): PlatformIO builds, flashes and
              # monitors it; on Linux this is an FHS environment so the toolchains
              # PlatformIO downloads into ~/.platformio run unmodified.
              pkgs.platformio
              pkgs.esptool # Standalone flashing, chip/flash info, erase
            ] ++ pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.procps ];

            # Avoid importing unrelated user-installed Python packages.
            PYTHONNOUSERSITE = "1";
            PC1_LIBUSB_LIBRARY = "${pkgs.libusb1}/lib/libusb-1.0${pkgs.stdenv.hostPlatform.extensions.sharedLibrary}";

            shellHook = ''
              echo "Paper Console: Python 3.12 + Node.js 22 (Nix-managed dependencies)"
              echo "Backend: just backend"
              echo "Tests:   just test"
              echo "UI:      just frontend-install && just frontend"
              echo "ESP32:   just fw-build / just fw-flash / just fw-monitor"
            '';
          };
        });
    };
}
