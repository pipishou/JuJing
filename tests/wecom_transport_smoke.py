#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企微传输层冒烟：用本地 HTTP server 验证 POST payload / 成功 / 失败路径（不连真实企微）。"""
import http.server
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.wecom_notify import send  # noqa: E402


class Handler(http.server.BaseHTTPRequestHandler):
    payload = None
    mode = "ok"

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        Handler.payload = json.loads(self.rfile.read(n).decode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if Handler.mode == "ok":
            self.wfile.write(json.dumps({"errcode": 0, "errmsg": "ok"}).encode())
        else:
            self.wfile.write(json.dumps({"errcode": 93000, "errmsg": "invalid webhook"}).encode())

    def log_message(self, *args):
        pass


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/send"

    rc = send("test-key", "## hello\n- x", False, url)
    assert rc == 0, f"ok path rc={rc}"
    assert Handler.payload and Handler.payload["msgtype"] == "markdown", "payload type"
    assert "hello" in Handler.payload["markdown"]["content"], "payload content"

    Handler.mode = "bad"
    rc = send("test-key", "## hello", False, url)
    assert rc == 1, f"error path rc={rc}"
    srv.shutdown()
    print(json.dumps({"ok": True, "port": port, "payload_ok": True, "error_path": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
