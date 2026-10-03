from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ashby.devices.ashby_devices import DEVICES  # noqa: E402
from secrets_store.env import ENDPOINT, ACCESS_ID, ACCESS_KEY, USERNAME, PASSWORD  # noqa: E402

LOGIN_PATH = "/v1.0/iot-01/associated-users/actions/authorized-login"

TARGETS = {
    "room_lights": ("captain_america_bulb", "thor_bulb", "sky_bulb"),
    "captain_america": ("captain_america_bulb",),
    "thor": ("thor_bulb",),
    "sky": ("sky_bulb",),
    "bed_lamp": ("bed_lamp",),
    "birdcage_light": ("birdcage_light",),
}


class TuyaClient:
    def __init__(self) -> None:
        self.endpoint = ENDPOINT.rstrip("/")
        self.access_id = ACCESS_ID
        self.access_key = ACCESS_KEY
        self.access_token = ""

    def _sign(self, method: str, path: str, body: dict | None = None) -> tuple[str, str, bytes | None]:
        content = "" if not body else json.dumps(body)
        body_bytes = content.encode("utf-8") if body else None
        string_to_sign = method + "\n" + hashlib.sha256(content.encode("utf-8")).hexdigest().lower() + "\n\n" + path
        timestamp = str(int(time.time() * 1000))
        message = self.access_id + self.access_token + timestamp + string_to_sign
        signature = hmac.new(self.access_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest().upper()
        return signature, timestamp, body_bytes

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        signature, timestamp, body_bytes = self._sign(method, path, body)
        headers = {
            "client_id": self.access_id,
            "sign": signature,
            "sign_method": "HMAC-SHA256",
            "access_token": self.access_token,
            "t": timestamp,
            "lang": "en",
            "Content-Type": "application/json",
        }
        if path == LOGIN_PATH:
            headers["dev_lang"] = "python"
            headers["dev_version"] = "ashby-scm-device-bridge"
            headers["dev_channel"] = ""

        request = urllib.request.Request(self.endpoint + path, data=body_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            return {"success": False, "http_status": exc.code, "msg": payload[:500]}
        except Exception as exc:
            return {"success": False, "msg": f"{type(exc).__name__}: {exc}"}

        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {"success": False, "msg": "invalid_json_response"}

    def connect(self) -> dict:
        body = {
            "username": USERNAME,
            "password": hashlib.md5(PASSWORD.encode("utf-8")).hexdigest(),
            "country_code": "1",
            "schema": "smartlife",
        }
        result = self._request("POST", LOGIN_PATH, body)
        if result.get("success"):
            self.access_token = str((result.get("result") or {}).get("access_token") or "")
        return result

    def command(self, device_id: str, commands: list[dict]) -> dict:
        if not self.access_token:
            connected = self.connect()
            if not connected.get("success"):
                return {"success": False, "stage": "connect", "msg": connected.get("msg"), "code": connected.get("code")}
        path = f"/v1.0/devices/{device_id}/commands"
        result = self._request("POST", path, {"commands": commands})
        if result.get("code") == 1010:
            self.access_token = ""
            connected = self.connect()
            if not connected.get("success"):
                return {"success": False, "stage": "reconnect", "msg": connected.get("msg"), "code": connected.get("code")}
            result = self._request("POST", path, {"commands": commands})
        return result


CLIENT = TuyaClient()


def commands_for(name: str, action: str, brightness_percent: int | None) -> list[dict]:
    category = str(DEVICES[name].get("category") or "")
    if category == "plug":
        if action == "set_brightness":
            raise ValueError(f"{name} does not support brightness")
        return [{"code": "switch_1", "value": action == "on"}]

    if action == "on":
        return [{"code": "switch_led", "value": True}]
    if action == "off":
        return [{"code": "switch_led", "value": False}]
    if action == "set_brightness":
        if brightness_percent is None:
            raise ValueError("brightness_percent is required")
        pct = max(1, min(100, int(brightness_percent)))
        return [
            {"code": "switch_led", "value": True},
            {"code": "bright_value_v2", "value": pct * 10},
        ]
    raise ValueError("unsupported action")


def execute(target: str, action: str, brightness_percent: int | None) -> dict:
    if target not in TARGETS:
        return {"ok": False, "status": "blocked", "error": "unsupported_target", "target": target}
    if action not in {"on", "off", "set_brightness"}:
        return {"ok": False, "status": "blocked", "error": "unsupported_action", "action": action}

    device_names = TARGETS[target]
    attempted: list[str] = []
    succeeded: list[str] = []
    failed: list[dict] = []

    for name in device_names:
        attempted.append(name)
        try:
            commands = commands_for(name, action, brightness_percent)
        except ValueError as exc:
            failed.append({"name": name, "code": None, "message": str(exc)})
            continue

        response = CLIENT.command(DEVICES[name]["id"], commands)
        if response.get("success"):
            succeeded.append(name)
        else:
            failed.append({
                "name": name,
                "code": response.get("code"),
                "message": response.get("msg") or response.get("stage") or "command_failed",
            })

    return {
        "ok": len(failed) == 0,
        "status": "completed" if not failed else ("partial" if succeeded else "failed"),
        "target": target,
        "action": action,
        "brightness_percent": brightness_percent if action == "set_brightness" else None,
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": failed,
    }


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(200, {"ok": True, "service": "ashby-device-bridge", "targets": sorted(TARGETS)})
            return
        self.send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/v1/control":
            self.send_json(404, {"ok": False, "error": "not_found"})
            return

        try:
            length = min(int(self.headers.get("Content-Length") or "0"), 4096)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            self.send_json(400, {"ok": False, "status": "blocked", "error": "invalid_json"})
            return

        target = str(payload.get("target") or "")
        action = str(payload.get("action") or "")
        brightness = payload.get("brightness_percent")
        if brightness is not None:
            try:
                brightness = int(brightness)
            except Exception:
                self.send_json(400, {"ok": False, "status": "blocked", "error": "invalid_brightness"})
                return
            if not 1 <= brightness <= 100:
                self.send_json(400, {"ok": False, "status": "blocked", "error": "brightness_out_of_range"})
                return

        result = execute(target, action, brightness)
        self.send_json(200 if result.get("status") in {"completed", "partial"} else 400, result)

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()


def main() -> None:
    host = os.environ.get("HOST") or os.environ.get("HOSTNAME") or "127.0.0.1"
    port = int(os.environ.get("PORT") or "8772")
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"Refusing non-loopback bind: {host}")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Ashby device bridge listening on http://127.0.0.1:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
