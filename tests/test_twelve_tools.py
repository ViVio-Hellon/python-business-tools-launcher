"""つなぐツールが12に増えたときに直したこと (1.7.10)

* **素性のファイルの名前がツールごとに違う。** Coil_Packaging-Specifications は
  `config/app_config.json` (`app_name`・`port`・`port_fallback_count`)、
  RNEConverter は `config/settings.json` (`app_id`・`display_name`・`server`)。
  以前は `config/app.json` しか読まず、「ブラウザーで開くツールはポートが
  要ります」で登録できなかった
* **Ainputcollect (VER2) は Web から tkinter の窓に作り直した。** `config/app.json`
  はあるがポートが無い。昔の `--no-browser` を受けて捨てるので、窓のツールと
  見分けられなかった
* **同じポートから始まるツールがある。** Display-file-list と excel-format-converter
  (8753)、CoilCalculator と RNEConverter (8741)。ツールはどれも、塞がっていれば
  次の空きポートへ移る。以前のランチャーは「ポート 8753 は、いま … が
  使っています」と**起動を断っていた**。ずらせるツールは起こし、移った先を
  ツールのプロセスの待ち受けから見つける。片方を止めても、もう片方は動いたまま
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fake_tool_support import free_port  # noqa: E402
from _isolation import LocalAreaTestCase, have_tkinter  # noqa: E402
from test_app_manager import ManagerTestCase  # noqa: E402
from test_new_tools import make_desktop_tool  # noqa: E402

from app_manager import State  # noqa: E402
from launcher import health, tool_registry  # noqa: E402
from launcher.tool_registry import UI_APP, Tool  # noqa: E402


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _tool_folder(base: Path, name: str) -> Path:
    root = base / name
    root.mkdir(parents=True)
    (root / "Start.vbs").write_text("' start\r\n", encoding="utf-8")
    return root / "Start.vbs"


class ConfigNameTests(LocalAreaTestCase):

    def test_app_config_jsonを読む(self) -> None:
        start = _tool_folder(self.work_root, "Coil_Packaging-Specifications")
        _write(start.parent / "config" / "app_config.json",
               {"app_id": "CoilPackagingSpec", "app_name": "コイル包装仕様表示",
                "version": "1.1.0", "host": "127.0.0.1", "port": 18765,
                "port_fallback_count": 5})
        found = tool_registry.probe_tool_folder(str(start))
        self.assertEqual((found["app_id"], found["display_name"], found["version"],
                          found["port"]),
                         ("CoilPackagingSpec", "コイル包装仕様表示", "1.1.0", 18765))
        tool = Tool(app_id="CoilPackagingSpec", display_name="コイル包装仕様表示",
                    port=18765, start_command=str(start))
        self.assertEqual(tool.ui_problem(), "")
        self.assertEqual(tool_registry.port_retry(tool), 5)

    def test_settings_jsonを読む(self) -> None:
        start = _tool_folder(self.work_root, "RNEConverter")
        _write(start.parent / "config" / "settings.json",
               {"app_id": "RNEConverter", "display_name": "RNE Converter (SQLite)",
                "server": {"port": 8741, "port_retry": 5, "open_browser": True},
                "extract": {"mode": "symnavi"}})
        found = tool_registry.probe_tool_folder(str(start))
        self.assertEqual((found["app_id"], found["display_name"], found["port"]),
                         ("RNEConverter", "RNE Converter (SQLite)", 8741))

    def test_app_idの無いsettings_jsonは使わない(self) -> None:
        start = _tool_folder(self.work_root, "どこか")
        _write(start.parent / "config" / "settings.json", {"server": {"port": 9000}})
        self.assertEqual(tool_registry.probe_tool_folder(str(start)), {})

    def test_app_jsonがあればそちらを使う(self) -> None:
        start = _tool_folder(self.work_root, "両方")
        _write(start.parent / "config" / "app.json",
               {"app_id": "nlm.both", "server": {"port": 8800}})
        _write(start.parent / "config" / "settings.json",
               {"app_id": "other", "server": {"port": 9000}})
        self.assertEqual(tool_registry.probe_tool_folder(str(start))["app_id"], "nlm.both")

    def test_ずらせる数を役割からも読む(self) -> None:
        start = _tool_folder(self.work_root, "excel-format-converter")
        _write(start.parent / "config" / "app.json",
               {"app_id": "nlm.excel-format-converter",
                "server": {"port_retry": 3, "roles": {"main": {"port": 8753}}}})
        tool = Tool(app_id="nlm.excel-format-converter", display_name="x", port=8753,
                    start_command=str(start))
        self.assertEqual(tool_registry.port_retry(tool), 3)
        self.assertEqual(tool_registry.port_retry(Tool(app_id="a", display_name="a")), 0)


class WindowToolWithAppJsonTests(LocalAreaTestCase):
    """Ainputcollect VER2: app.json はあるがポートが無い・tkinter・--no-browser を受けて捨てる。"""

    def make(self, server=None) -> Path:
        start = make_desktop_tool(self.work_root, "ainputcollect", readme="# アイカス\n")
        start = start.with_name("Start.vbs")
        start.write_text("' pythonw start_app.py\r\n", encoding="utf-8")
        (start.parent / "start_app.py").write_text(
            "import tkinter\n"
            "parser.add_argument('--no-browser', action='store_true')  # 受けて捨てる\n",
            encoding="utf-8")
        data = {"app_id": "nlm.ainput-collect", "display_name": "アイカス実績照会",
                "version": "2.5.0"}
        if server:
            data["server"] = server
        _write(start.parent / "config" / "app.json", data)
        return start

    def test_ポートの無いapp_jsonなら窓のツール(self) -> None:
        found = tool_registry.probe_tool_folder(str(self.make()))
        self.assertEqual(found["ui_mode"], UI_APP)
        self.assertEqual((found["app_id"], found["display_name"]),
                         ("nlm.ainput-collect", "アイカス実績照会"))
        args, _ = tool_registry.recommend_start_args(str(self.make_again()))
        self.assertEqual(args, "")

    def make_again(self) -> Path:
        return self.work_root / "ainputcollect" / "Start.vbs"

    def test_ポートがあればWebのツールのまま(self) -> None:
        found = tool_registry.probe_tool_folder(str(self.make({"port": 8733})))
        self.assertNotIn("ui_mode", found)
        self.assertEqual(found["port"], 8733)


class SamePortTests(ManagerTestCase):
    """同じポートから始まる2つのツールを、同時に使う。"""

    def test_ずれるツールは断らずに起こし移った先で動かす(self) -> None:
        port = free_port()
        files = self.register("nlm.file-list-tool", "ファイル一覧", port=port, port_retry=5)
        self.start(files)
        self.assertEqual(self.manager.status.state, State.RUNNING)
        self.assertEqual(self.manager.running[files.app_id].port, port)

        excel = self.register("nlm.excel-format-converter", "Excel変換", port=port,
                              port_retry=3)
        self.start(excel)
        status = self.manager.status
        self.assertEqual(status.state, State.RUNNING, status.detail)
        moved = self.manager.running[excel.app_id]
        self.assertNotEqual(moved.port, port)
        self.assertTrue(health.is_tool(health.probe(moved.health_url), excel.app_id))
        # ツールの作りどおりの動きなので、設定の直しを勧めない
        self.assertIn(f"{moved.port} で開きました", status.detail)
        self.assertIn("設定は直さなくて大丈夫", status.detail)
        # 画面も移った先を開く
        self.assertIn(f":{moved.port}/", self.opened[-1])

        # 見回りで、片方をもう片方と取り違えない
        for _ in range(3):
            self.manager.poll_health()
        self.assertEqual(set(self.manager.running), {files.app_id, excel.app_id})

        # もう一度押しても2つ目を起こさない
        self.start(excel)
        self.assertEqual(self.manager.running[excel.app_id].port, moved.port)

        # 移ったほうを止めても、先に動いていたほうは動いたまま
        self.assertTrue(self.manager._stop_blocking(excel.app_id))
        self.assertIsNone(health.probe(moved.health_url))
        self.assertTrue(health.is_tool(
            health.probe(f"http://127.0.0.1:{port}/api/health"), files.app_id))
        self.assertIn(files.app_id, self.manager.running)

    def test_先に動いていたほうを止めても移ったほうは動いたまま(self) -> None:
        port = free_port()
        coil = self.register("nlm.coil-calculator", "VC長さ", port=port, port_retry=5)
        rne = self.register("RNEConverter", "RNE Converter", port=port, port_retry=5)
        self.start(coil)
        self.start(rne)
        moved = self.manager.running[rne.app_id]
        self.assertNotEqual(moved.port, port)
        self.assertTrue(self.manager._stop_blocking(coil.app_id))
        deadline = time.monotonic() + 3
        while health.probe(f"http://127.0.0.1:{port}/api/health") and \
                time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(health.is_tool(health.probe(moved.health_url), rne.app_id))
        self.manager.poll_health()
        self.assertIn(rne.app_id, self.manager.running)

    def test_ずれないツールはこれまでどおり押した時点で断る(self) -> None:
        port = free_port()
        first = self.register("fake.first", "看板", port=port, port_retry=3)
        self.start(first)
        fixed = self.register("fake.fixed", "日報", port=port)
        self.start(fixed)
        self.assertEqual(self.manager.status.state, State.ERROR)
        self.assertIn("いま「看板」が使っています", self.manager.status.detail)


@unittest.skipUnless(have_tkinter(), "設定画面の部品が tkinter を読み込む")
class SamePortWarningTests(LocalAreaTestCase):

    def tool(self, name: str, app_id: str, retry) -> Tool:
        start = _tool_folder(self.work_root, name)
        server = {"port": 8753}
        if retry is not None:
            server["port_retry"] = retry
        _write(start.parent / "config" / "app.json", {"app_id": app_id, "server": server})
        return Tool(app_id=app_id, display_name=name, port=8753, start_command=str(start))

    def test_どれもずれるなら保存のとき知らせない(self) -> None:
        from launcher.ui.settings_dialog import _shared_port_warnings

        tools = [self.tool("ファイル名一覧取得", "nlm.file-list-tool", 5),
                 self.tool("作業標準書 書式変換", "nlm.excel-format-converter", 3)]
        self.assertEqual(_shared_port_warnings(tools), [])

    def test_ずれないものがあれば知らせる(self) -> None:
        from launcher.ui.settings_dialog import _shared_port_warnings

        tools = [self.tool("ファイル名一覧取得", "nlm.file-list-tool", 5),
                 self.tool("古いツール", "old.tool", None)]
        self.assertEqual(len(_shared_port_warnings(tools)), 1)


if __name__ == "__main__":
    unittest.main()
