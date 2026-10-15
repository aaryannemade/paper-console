"""The server exposes no WiFi provisioning or captive-portal surface."""

from pathlib import Path

import app.main as main

RETIRED_PATHS = {
    "/hotspot-detect.html",
    "/library/test/success.html",
    "/success.html",
    "/generate_204",
    "/gen_204",
    "/mobile/status.php",
    "/connecttest.txt",
    "/ncsi.txt",
    "/redirect",
    "/success.txt",
    "/canonical.html",
    "/check_network_status.txt",
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _route_paths():
    return {getattr(route, "path", "") for route in main.app.routes}


def test_wifi_api_and_captive_probe_routes_are_not_registered():
    paths = _route_paths()

    assert not any(path.startswith("/api/wifi") for path in paths)
    assert paths.isdisjoint(RETIRED_PATHS)


def test_frontend_has_no_catch_all_route_for_retired_probe_paths():
    # A catch-all would serve index.html for retired probes instead of a 404.
    assert "/{full_path:path}" not in _route_paths()


def test_server_wifi_manager_and_router_are_removed():
    assert not (PROJECT_ROOT / "app" / "wifi_manager.py").exists()
    assert not (PROJECT_ROOT / "app" / "routers" / "wifi.py").exists()


def test_frontend_does_not_call_wifi_endpoints():
    sources = [
        path.read_text(encoding="utf-8")
        for path in (PROJECT_ROOT / "web" / "src").rglob("*")
        if path.suffix in {".js", ".jsx", ".ts", ".tsx"}
    ]

    assert not any("/api/wifi" in source for source in sources)
    assert not (PROJECT_ROOT / "web" / "src" / "WiFiSetup.jsx").exists()
