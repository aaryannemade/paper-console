{
  description = "Paper Console development environment (console, USB and ESP32 relay)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { nixpkgs, ... }:
    let
      # Current nixpkgs no longer supports Intel macOS.
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          # npm's latest release is newer than the copy bundled with Node.
          npm = pkgs.stdenvNoCC.mkDerivation {
            pname = "npm";
            version = "12.2.0";
            src = pkgs.fetchurl {
              url = "https://registry.npmjs.org/npm/-/npm-12.2.0.tgz";
              hash = "sha256-Zma0iBazm4bD/rrHtRpO5N5sXKWJw4KtgAS2sRP4Znc=";
            };
            nativeBuildInputs = [ pkgs.makeWrapper ];
            dontBuild = true;
            installPhase = ''
              mkdir -p $out/lib/node_modules/npm $out/bin
              cp -r . $out/lib/node_modules/npm/
              for command in npm npx; do
                makeWrapper ${pkgs.nodejs_26}/bin/node $out/bin/$command \
                  --add-flags "$out/lib/node_modules/npm/bin/$command-cli.js"
              done
            '';
          };
          # Python 3.15's Nix dependency closure currently fails to build (tkinter).
          # Keep this minor version aligned with Dockerfile.
          pythonBase = pkgs.python314;
          python = pythonBase.withPackages (ps: with ps; [
            (pip.overridePythonAttrs (old: {
              version = "26.2.1";
              src = pkgs.fetchPypi {
                pname = "pip";
                version = "26.2.1";
                sha256 = "f6ad667e89a1fe78046c8f13232b247200f5258d7828f3f7883d660878e0813f";
              };
              # PyPI provides an archive, not the unpacked GitHub source tree.
              postBuild = builtins.replaceStrings [ "$src/src" ] [ "$PWD/../src" ] old.postBuild;
            }))
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
              npm
              (pkgs.lib.lowPrio pkgs.nodejs_26)
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
            ] ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [ pkgs.procps ];

            # Avoid importing unrelated user-installed Python packages.
            PYTHONNOUSERSITE = "1";
            PC1_LIBUSB_LIBRARY = "${pkgs.libusb1}/lib/libusb-1.0${pkgs.stdenv.hostPlatform.extensions.sharedLibrary}";

            shellHook = ''
              echo "Paper Console: Python ${pythonBase.version} + Node.js ${pkgs.nodejs_26.version} (Nix-managed dependencies)"
              echo "Dev:     just dev-console / just dev-usb / just dev-esp32 (login 123456789)"
              echo "Tests:   just test"
              echo "UI deps: just frontend-install"
              echo "ESP32:   just fw-build / just fw-flash / just fw-monitor"
            '';
          };
        });
    };
}
