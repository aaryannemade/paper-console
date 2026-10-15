{
  description = "Paper Console development environment (mock hardware)";

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
            pytz
            python-dateutil
            astral
            beautifulsoup4
            croniter
            pyserial
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
              pkgs.bash
              pkgs.lsof
            ] ++ pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.procps ];

            # Avoid importing unrelated user-installed Python packages.
            PYTHONNOUSERSITE = "1";

            shellHook = ''
              echo "Paper Console: Python 3.12 + Node.js 22 (Nix-managed dependencies)"
              echo "Backend: python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload"
              echo "Tests:   python -m pytest -q testing"
              echo "UI:      npm --prefix web ci && npm --prefix web run dev"
            '';
          };
        });
    };
}
