"""/api/health も素性のファイルも無い Web のツール (accdb_converter。1.8.6)

accdb_converter (DB 変換 & ビューア) は Flask + waitress で、ポート 5000 をプログラムに
決め打ちしている。`config/app.json` も `/api/health` も `--no-browser` も無い。

* ［＋ ツールを追加］で、プログラムに書いてあるポートと README の名前を読む
* 起動と生死は「**そのツールのプロセスが**ポートで待ち受けているか」で確かめる
  (アプリIDを照合できないので、プロセスで本人と確かめる。ほかのアプリを取り違えない)
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fake_tool_support import free_port  # noqa: E402
from _isolation import LocalAreaTestCase  # noqa: E402
from test_app_manager import ManagerTestCase  # noqa: E402

import process_manager  # noqa: E402
from app_manager import State  # noqa: E402
from launcher import tool_registry  # noqa: E402
from launcher.tool_registry import Tool  # noqa: E402

SERVER = '''# -*- coding: utf-8 -*-
"""run_waitress.py の代わり。標準ライブラリだけで同じポートを待ち受ける (http.server)。"""
import http.server

HOST = "127.0.0.1"
PORT = {port}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<html>DB</html>")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    http.server.HTTPServer((HOST, PORT), Handler).serve_forever()
'''

README = """====================================================================
 DB 変換 & ビューア  (accdb/xlsx/sqlite  相互変換 + データ確認)
====================================================================
"""


def make_plain_web(base: Path, *, port: int, name: str = "accdb_converter") -> Path:
    root = base / name
    root.mkdir(parents=True)
    (root / "run_waitress.py").write_text(SERVER.format(port=port), encoding="utf-8")
    (root / "README.txt").write_text(README, encoding="utf-8")
    start = root / "start.bat"
    # 本物の start.bat と同じく、本体が終わるまで待つ (exec しない)
    start.write_text(f'#!/bin/sh\ncd "$(dirname "$0")"\n"{sys.executable}" run_waitress.py\n',
                     encoding="utf-8")
    os.chmod(start, 0o755)
    return start


class ProbeTests(LocalAreaTestCase):

    def test_ポートと名前を読む(self) -> None:
        start = make_plain_web(self.work_root, port=5000)
        found = tool_registry.probe_tool_folder(str(start))
        self.assertEqual(found, {"port": 5000, "kind": "plain-web",
                                 "display_name": "DB 変換 & ビューア",
                                 "app_id": "web.accdb_converter"})
        args, _ = tool_registry.recommend_start_args(str(start))
        self.assertEqual(args, "")

    def test_ポートとプロセスで確かめるのは素性のファイルが無いときだけ(self) -> None:
        start = make_plain_web(self.work_root, port=5000)
        tool = Tool(app_id="web.accdb_converter", display_name="DB", port=5000,
                    start_command=str(start))
        self.assertTrue(tool_registry.confirms_by_port(tool))
        (start.parent / "config").mkdir()
        (start.parent / "config" / "app.json").write_text(
            '{"app_id": "nlm.accdb", "server": {"port": 5000}}', encoding="utf-8")
        self.assertFalse(tool_registry.confirms_by_port(tool))

    def test_Webの部品が無ければ見分けない(self) -> None:
        start = make_plain_web(self.work_root, port=5000)
        (start.parent / "run_waitress.py").write_text("PORT = 5000\nprint(PORT)\n",
                                                      encoding="utf-8")
        self.assertEqual(tool_registry.probe_tool_folder(str(start)), {})


class LifecycleTests(ManagerTestCase):

    def test_起動して見回りで外さず止められる(self) -> None:
        port = free_port()
        start = make_plain_web(self.work_root, port=port)
        found = tool_registry.probe_tool_folder(str(start))
        tool_registry.save(Tool(app_id=found["app_id"], display_name=found["display_name"],
                                port=found["port"], start_command=str(start)))
        tool = tool_registry.get("web.accdb_converter")
        self.start(tool)
        status = self.manager.status
        self.assertEqual(status.state, State.RUNNING, status.detail)
        running = self.manager.running[tool.app_id]
        self.assertTrue(running.by_port)
        self.assertEqual(running.health_url, "")
        for _ in range(2):
            self.manager.poll_health()
        self.assertIn(tool.app_id, self.manager.running, "見回りで外しました")
        self.assertTrue(self.manager._stop_blocking(tool.app_id), self.manager.status.detail)
        self.assertFalse(process_manager.is_running(running))

    def test_同じポートをほかのアプリが使っていても取り違えない(self) -> None:
        import socket

        port = free_port()
        other = socket.socket()
        other.bind(("127.0.0.1", port))
        other.listen()
        self.addCleanup(other.close)
        start = make_plain_web(self.work_root, port=port)
        tool = Tool(app_id="web.accdb_converter", display_name="DB", port=port,
                    start_command=str(start))
        # ツールのプロセスでなければ、待ち受けがあっても本人ではない
        self.assertFalse(process_manager.port_owned(tool, {os.getpid() + 999999}))


if __name__ == "__main__":
    unittest.main()
