"""本机只读面板：只把计划、排程与执行队列摆到页面上，不联网、不写计划。

面板只监听 127.0.0.1，并校验 Host，避免外部域名解析到本机后读走计划（DNS rebinding）。
"""

from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlsplit

from trainops_base import PlanError
from trainops_graph import snapshot


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, store, **kwargs):
        self.store = store
        super().__init__(*args, **kwargs)

    def view(self):
        """面板看到的计划：所有派生字段都由 snapshot() 算好，前端不做业务计算。"""
        return snapshot(self.store.read())

    def respond(self, status, data, content_type="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def local(self):
        """只认 127.0.0.1/localhost：外部域名解析到本机也不能读走计划（防 DNS rebinding）。"""
        hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.server.server_port == 80:
            hosts.update({"127.0.0.1", "localhost"})
        return self.headers.get("Host", "").lower() in hosts

    def do_GET(self):
        if not self.local():
            self.respond(403, {"error": "只允许本机 Host，不能通过外部域名访问计划"})
            return
        try:
            if urlsplit(self.path).path == "/api/plan":
                self.respond(200, self.view())
            elif urlsplit(self.path).path == "/":
                page = Path(__file__).resolve().parent.parent / "web" / "index.html"
                self.respond(200, page.read_bytes(), "text/html; charset=utf-8")
            else:
                self.respond(404, {"error": "not found"})
        except (PlanError, OSError, OverflowError) as exc:
            self.respond(500, {"error": str(exc)})

    def log_message(self, format, *args):
        pass


def server(store, port=8765):
    return ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, store=store))


__all__ = ["Handler", "server"]
