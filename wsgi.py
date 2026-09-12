import json
import os
from urllib.parse import urlparse
from sensor_server import generator, real_bridge, HEALTHY, HTML_FILE, HTML_MOBILE, HTML_CONTROL

def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = urlparse(environ.get("PATH_INFO", "/") or "/").path

    def respond(status, ctype, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = [
            ("Content-Type", ctype),
            ("Content-Length", str(len(body))),
            ("Access-Control-Allow-Origin", "*"),
            ("Cache-Control", "no-cache, no-store, must-revalidate"),
        ]
        start_response(status, headers)
        return [body]

    def serve_file(filepath):
        try:
            with open(filepath, "rb") as f:
                body = f.read()
            headers = [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-cache"),
            ]
            start_response("200 OK", headers)
            return [body]
        except FileNotFoundError:
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"Archivo no encontrado"]

    if method in ("GET", "HEAD") and path == "/api/data":
        data = generator.get_data()
        if real_bridge.is_active:
            data["patient"] = real_bridge.blend_patient(data["patient"])
            data["breathing"] = real_bridge.blend_breathing(data["breathing"])
            data["source"] = "esp32_real"
        else:
            data["source"] = "simulation"
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if method == "HEAD":
            start_response("200 OK", [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body))), ("Access-Control-Allow-Origin", "*")])
            return [b""]
        return respond("200 OK", "application/json; charset=utf-8", body)

    if method in ("GET", "HEAD") and path == "/api/healthy":
        return respond("200 OK", "application/json; charset=utf-8", json.dumps(HEALTHY))

    if method in ("GET", "HEAD") and path == "/api/esp32-status":
        return respond("200 OK", "application/json; charset=utf-8", json.dumps({
            "connected": real_bridge.is_active,
            "last_rx": real_bridge.last_rx_time,
            "esp32_ip": real_bridge.esp32_ip,
        }))

    if method in ("GET", "HEAD") and path == "/api/control":
        return respond("200 OK", "application/json; charset=utf-8", json.dumps({
            "control": generator.get_control_state(),
            "current": generator.patient,
            "goldStage": generator.gold_stage,
            "severity": generator.severity,
        }))

    if method == "POST" and path == "/api/control":
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
            body = environ["wsgi.input"].read(length) if length > 0 else b"{}"
            payload = json.loads(body.decode("utf-8")) if body else {}
            result = generator.set_control(payload)
            return respond("200 OK", "application/json; charset=utf-8", json.dumps({"status": "ok", "control": result}))
        except Exception as e:
            return respond("200 OK", "application/json; charset=utf-8", json.dumps({"status": "error", "msg": str(e)}))

    if method == "POST" and path == "/api/esp32":
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
            body = environ["wsgi.input"].read(length) if length > 0 else b""
            if not body:
                return respond("200 OK", "application/json; charset=utf-8", json.dumps({"status": "error", "msg": "empty body"}))
            data = json.loads(body.decode("utf-8"))
            data["esp32_ip"] = environ.get("REMOTE_ADDR", "")
            real_bridge.update(data)
            n = data.get("samples", 0)
            if not isinstance(n, int):
                n = len(n) if hasattr(n, "__len__") else 0
            return respond("200 OK", "application/json; charset=utf-8", json.dumps({"status": "ok", "samples": n}))
        except Exception as e:
            return respond("200 OK", "application/json; charset=utf-8", json.dumps({"status": "error", "msg": str(e)}))

    if method == "OPTIONS":
        start_response("204 No Content", [
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
            ("Content-Length", "0"),
        ])
        return [b""]

    if method in ("GET", "HEAD") and path == "/":
        if method == "HEAD":
            try:
                sz = os.path.getsize(HTML_FILE)
                start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(sz)), ("Cache-Control", "no-cache")])
                return [b""]
            except: pass
        return serve_file(HTML_FILE)

    if method in ("GET", "HEAD") and path == "/mobile":
        if method == "HEAD":
            try:
                sz = os.path.getsize(HTML_MOBILE)
                start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(sz)), ("Cache-Control", "no-cache")])
                return [b""]
            except: pass
        return serve_file(HTML_MOBILE)

    if method in ("GET", "HEAD") and path == "/control":
        if method == "HEAD":
            try:
                sz = os.path.getsize(HTML_CONTROL)
                start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(sz)), ("Cache-Control", "no-cache")])
                return [b""]
            except: pass
        return serve_file(HTML_CONTROL)

    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"Not found"]
