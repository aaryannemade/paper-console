"""Send ESP32-style input events to a running server (development helper).

Acts as the device's controls without hardware. Uses the
shared token from PC1_RELAY_TOKEN.

  python testing/device_event.py position 5        # select channel 5
  python testing/device_event.py press             # short press (print / choose)
  python testing/device_event.py press --position 3
  python testing/device_event.py hold              # 5 s reached: cancel print or arm menu
  python testing/device_event.py quick-actions     # hold, then release (opens Quick Actions)
  python testing/device_event.py cancel            # hold during a print (same as 'hold')
  python testing/device_event.py hold-cancel       # hold, then the device keeps the press

Options: --url (default http://127.0.0.1:8000), --token (default $PC1_RELAY_TOKEN).
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

# One press id per invocation; gestures of the same physical press share it.
SESSION = f"cli{random.randrange(16**6):06x}"


def send(url, token, event):
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/device/events",
        data=json.dumps(event).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"Cannot reach {url}: {exc.reason}", file=sys.stderr)
        sys.exit(1)
    label = event.get("gesture") or event["type"]
    print(f"{label:13} -> {body['result']} (position {body['position']})")
    return body


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "action",
        choices=["position", "press", "hold", "cancel", "quick-actions", "hold-cancel"],
    )
    parser.add_argument("value", nargs="?", type=int, help="position for 'position'")
    parser.add_argument("--position", type=int, help="position to report with a button event")
    parser.add_argument("--url", default=os.environ.get("PC1_SERVER_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("PC1_RELAY_TOKEN", ""))
    args = parser.parse_args()

    if not args.token:
        parser.error("set PC1_RELAY_TOKEN or pass --token")

    counter = iter(range(1, 100))

    def button(gesture):
        event = {
            "event_id": f"{SESSION}-{next(counter)}",
            "type": "button",
            "gesture": gesture,
            "press_id": SESSION,
        }
        if args.position is not None:
            event["position"] = args.position
        return send(args.url, args.token, event)

    if args.action == "position":
        if args.value is None:
            parser.error("position needs a value 1-8")
        send(args.url, args.token,
             {"event_id": f"{SESSION}-pos", "type": "position", "position": args.value})
    elif args.action == "press":
        button("press")
    elif args.action in ("hold", "cancel"):
        button("hold")
    elif args.action == "quick-actions":
        if button("hold")["result"] == "hold_ready":
            time.sleep(0.2)
            button("hold_release")
    elif args.action == "hold-cancel":
        # Device kept the press (reset sequence): no Quick Actions afterwards.
        button("hold")
        button("hold_cancel")


if __name__ == "__main__":
    main()
