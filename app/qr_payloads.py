"""QR content encoders, independent of server or device network management."""


def generate_wifi_qr_payload(
    ssid: str,
    password: str,
    security: str = "WPA",
    hidden: bool = False,
) -> str:
    """Encode user-supplied WiFi credentials for phone camera scanners."""
    def escape(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace(":", "\\:")
            .replace('"', '\\"')
        )

    hidden_field = ";H:true" if hidden else ""
    if security.upper() == "NOPASS":
        return f"WIFI:S:{escape(ssid)};T:nopass{hidden_field};;"
    return f"WIFI:S:{escape(ssid)};T:{security};P:{escape(password)}{hidden_field};;"
