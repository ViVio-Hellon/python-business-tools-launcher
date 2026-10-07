"""試験用の偽のデスクトップアプリ (Tauri などで作った exe の代わり)

ランチャーから見たデスクトップアプリの約束事だけを守る:

    起動すると窓を出して動き続ける (ここでは「生きている」ことが窓の代わり)
    窓を閉じるよう頼まれたら終わる (Windows の WM_CLOSE → ここでは SIGTERM)
    戻り値 0 で終われば閉じた、それ以外は落ちた

`--port` を渡すと、中に Web サーバーを持つアプリ (Tauri + Python の
サーバー部分) として `/api/health` も返す。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

START_AT = time.monotonic()


def _serve(options: argparse.Namespace) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def do_GET(self) -> None:
            if not self.path.startswith("/api/health") or options.no_health:
                self.send_response(404)
                self.end_headers()
                return
            payload = {
                "app_id": options.app_id, "pid": os.getpid(),
                "port": options.port, "version": "2.0.0",
                "ready": time.monotonic() - START_AT >= options.ready_after,
            }
            if options.no_port_in_health:
                del payload["port"]
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", options.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ready-after", type=float, default=0.0)
    # すぐこの戻り値で終わる (窓を出す前に落ちる)
    parser.add_argument("--exit-code", type=int)
    # しばらく動いてから落ちる (Rust の panic は 101)
    parser.add_argument("--crash-after", type=float)
    parser.add_argument("--crash-code", type=int, default=101)
    # 閉じるよう頼まれても終わらない (「保存しますか」を出して待っている)
    parser.add_argument("--ask-on-close", action="store_true")
    # Web サーバーが答えなくなった状態 (窓は出たまま)
    parser.add_argument("--no-health", action="store_true")
    # 起動確認の応答に port を入れない (入れないツールもある)
    parser.add_argument("--no-port-in-health", action="store_true")
    # Python から作ったサーバーの exe と同じく、受け取って無視する
    parser.add_argument("--no-browser", action="store_true")
    # 起動用の exe の真似: 本体を別のプロセスとして起こし、自分は戻り値 0 で
    # すぐ終わる (梱包資材総合ツール.exe がこの作り)
    parser.add_argument("--detach", action="store_true")
    options = parser.parse_args(argv)

    if options.detach and not os.environ.get("FAKE_APP_DETACHED"):
        import subprocess
        # 本体は同じ exe (ツールのフォルダーの中) を、印を付けて起こす
        subprocess.Popen([sys.executable, sys.argv[0]] + sys.argv[1:],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True,
                         env={**os.environ, "FAKE_APP_DETACHED": "1"})
        return 0

    if options.exit_code is not None:
        print("Error: WebView2 を初期化できませんでした", flush=True)
        return options.exit_code

    if options.ask_on_close:
        signal.signal(signal.SIGTERM, lambda *_: print("保存しますか?", flush=True))
    else:
        signal.signal(signal.SIGTERM, lambda *_: os._exit(0))

    if options.port:
        _serve(options)
    print(f"{options.app_id} の窓を出しました", flush=True)

    while True:
        time.sleep(0.1)
        if options.crash_after is not None \
                and time.monotonic() - START_AT >= options.crash_after:
            print("thread 'main' panicked at src/main.rs:42:9", flush=True)
            os._exit(options.crash_code)


if __name__ == "__main__":
    raise SystemExit(main())
