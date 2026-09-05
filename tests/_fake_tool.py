"""試験用の偽の業務ツール。

本物の4ツールと同じ約束事だけを守る、いちばん小さいWebサーバ:

    GET  /api/health    … app_id / pid / port / ready を返す
    POST /api/shutdown  … 正常終了する (実行中の処理があれば 409)

本物は Flask で動くが、ランチャーが見ているのは**この2つの窓口だけ**
なので、試験では標準ライブラリで足りる。追加パッケージを入れずに
起動・切替・停止の全体を通せる。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    options: argparse.Namespace

    def log_message(self, *args) -> None:      # 標準エラーを汚さない
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        opt = self.options
        if self.path.startswith("/api/health"):
            self._send(200, {
                "app_id": opt.app_id,
                "display_name": opt.display_name,
                "version": "1.0.0",
                "app_root": opt.app_root,
                "port": opt.port,
                "pid": os.getpid(),
                "ready": time.monotonic() - START_AT >= opt.ready_after,
                "stage": "アプリを準備中",
            })
            return
        self._send(200, {"ok": True})

    def do_POST(self) -> None:
        opt = self.options
        if not self.path.startswith("/api/shutdown"):
            self._send(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            force = bool(json.loads(body.decode("utf-8")).get("force"))
        except ValueError:
            force = False

        if opt.busy and not force:
            # 本物と同じ形で断る (基盤仕様書 2.8)
            self._send(409, {"stopped": False, "reason": "busy",
                             "running": ["取り込み"],
                             "message": "実行中の処理があります"})
            return

        self._send(200, {"stopped": True, "message": "終了します"})
        threading.Timer(0.2, lambda: os._exit(0)).start()


START_AT = time.monotonic()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--display-name", default="偽ツール")
    parser.add_argument("--app-root", default=os.getcwd())
    parser.add_argument("--ready-after", type=float, default=0.0)
    parser.add_argument("--busy", action="store_true")
    # 本物が受け付ける引数。受け取って無視する
    parser.add_argument("--no-browser", action="store_true")
    options = parser.parse_args(argv)

    Handler.options = options
    server = ThreadingHTTPServer(("127.0.0.1", options.port), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
