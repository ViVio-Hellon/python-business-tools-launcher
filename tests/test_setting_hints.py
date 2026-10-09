"""［設定］のヒント (どう設定するのがよいか) と、入口が答えた画面の URL

現場の4ツール (all-tools・python-web-tools・CoilCalculator・coil-packing-tools)
の置き方を、各リポジトリの実物 (起動ファイル・入口・config/app.json) どおりに
作って確かめる。ヒントはツールごとの特別扱いではなく、**起動ファイルとその
隣にあるもの**から出す (ランチャー連携 §2)。
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase, release_tk  # noqa: E402
from test_app_manager import ManagerTestCase  # noqa: E402
from test_tool_entries import _script, make_entry_tool  # noqa: E402

from app_manager import State  # noqa: E402
from launcher import tool_registry  # noqa: E402
from launcher.tool_registry import Tool  # noqa: E402

VBS_NO_ARGS = 'shell.Run "pythonw " & Chr(34) & script & Chr(34), 0, False\n'
VBS_ARGS = ('For i = 0 To WScript.Arguments.Count - 1\n'
            '  cmd = cmd & " " & WScript.Arguments(i)\nNext\n')


def make_tool(base: Path, name: str, *, exe: str = "", server: dict,
              app_id: str, vbs: str = VBS_NO_ARGS, entries: bool = False) -> Path:
    """現場のツールのフォルダー (exe・Start.vbs・start.bat・stop.bat・config/app.json)。"""
    root = base / name
    (root / "config").mkdir(parents=True)
    (root / "config" / "app.json").write_text(json.dumps(
        {"app_id": app_id, "display_name": name, "server": server},
        ensure_ascii=False), encoding="utf-8")
    if exe:
        (root / exe).write_bytes(b"MZ" + b"\x00" * 64)
    (root / "Start.vbs").write_text(vbs, encoding="utf-8")
    (root / "start.bat").write_text("@echo off\npython start_app.py %*\n", encoding="utf-8")
    (root / "stop.bat").write_text("@echo off\npython process_manager.py %*\n",
                                   encoding="utf-8")
    (root / "start_app.py").write_text('p.add_argument("--no-browser")\n', encoding="utf-8")
    if entries:
        for entry in ("launcher_check.bat", "launcher_stop.bat"):
            (root / entry).write_text("@echo off\n", encoding="utf-8")
    return root


class HintTestCase(LocalAreaTestCase):

    def hints(self, path: Path, **fields) -> str:
        found = tool_registry.probe_tool_folder(str(path))
        tool = Tool(app_id=fields.pop("app_id", found.get("app_id", "x")),
                    display_name="試験", start_command=str(path),
                    port=fields.pop("port", int(found.get("port") or 0)), **fields)
        return "\n".join(tool_registry.setting_hints(tool, probed=found))


class AllToolsTests(HintTestCase):
    """all-tools: exe・Start.vbs、入口あり (launcher_check.bat・launcher_stop.bat)。"""

    def setUp(self) -> None:
        super().setUp()
        self.root = make_tool(self.work_root, "統合ツール", exe="統合ツール.exe",
                              app_id="nlm.all-tools", entries=True,
                              server={"port": 8700, "port_retry": 9})

    def test_入口があればポートと停止方法は自動のままでよい(self) -> None:
        for start in ("統合ツール.exe", "Start.vbs"):
            with self.subTest(start=start):
                text = self.hints(self.root / start)
                self.assertIn("止め方をツール側で用意しています", text)
                self.assertIn("「自動」のままで大丈夫です", text)
                self.assertNotIn("ポートは空にしてください", text)

    def test_中のツールは登録しないよう知らせる(self) -> None:
        inner = self.root / "tools" / "nippou"
        inner.mkdir(parents=True)
        (inner / "Start.vbs").write_text(VBS_NO_ARGS, encoding="utf-8")
        tool_registry.save(Tool(app_id="nlm.all-tools", display_name="統合ツール",
                                start_command=str(self.root / "統合ツール.exe")))
        text = self.hints(inner / "Start.vbs", app_id="nlm.nippou", port=8733)
        self.assertIn("「統合ツール」の中にあります", text)
        self.assertIn("ここには登録しないでください", text)


class PackagingToolTests(HintTestCase):
    """python-web-tools: 入口なし。役割でポートが違う (現場 8713 / 資材 8723)。"""

    def setUp(self) -> None:
        super().setUp()
        self.root = make_tool(
            self.work_root, "梱包資材総合ツール", exe="梱包資材総合ツール.exe",
            app_id="nlm.packaging-tool",
            server={"port_retry": 3, "roles": {"field": {"port": 8713},
                                               "material": {"port": 8723}}})

    def test_exeの行にブラウザー版のポートがあれば空にするよう言う(self) -> None:
        text = self.hints(self.root / "梱包資材総合ツール.exe", port=8713)
        self.assertIn("ポートは空にしてください (8713 はブラウザー版の番号", text)
        self.assertIn("窓の × を押したのと同じように閉じます", text)

    def test_exeの行がポート空ならポートのことは言わない(self) -> None:
        text = self.hints(self.root / "梱包資材総合ツール.exe", port=0)
        self.assertNotIn("ポートは空に", text)

    def test_Startvbsでは役割のポートと画面の開き方を言う(self) -> None:
        text = self.hints(self.root / "Start.vbs")
        self.assertIn("この PC の役割の番号を入れてください (field 8713 / material 8723)", text)
        self.assertIn("ふだんのブラウザーのタブに開きます", text)
        self.assertIn("ツールに付いている stop.bat を実行して止めます", text)

    def test_startbatならStartvbsを勧める(self) -> None:
        text = self.hints(self.root / "start.bat")
        self.assertIn("同じフォルダーの Start.vbs を選んでください", text)


class CoilCalculatorTests(HintTestCase):
    """CoilCalculator: 入口あり。Start.vbs は引数を渡す。"""

    def test_引数を渡すStartvbsでは画面の開き方を言わない(self) -> None:
        root = make_tool(self.work_root, "CoilCalculator", exe="CoilCalculator.exe",
                         app_id="nlm.coil-calculator", vbs=VBS_ARGS, entries=True,
                         server={"port": 8741, "port_retry": 5})
        text = self.hints(root / "Start.vbs")
        self.assertIn("止め方をツール側で用意しています", text)
        self.assertNotIn("ふだんのブラウザーのタブに開きます", text)

    def test_ランチャー専用の窓はexeではないと言う(self) -> None:
        """アドレスバーの無い専用の窓は、exe 版の窓と見分けにくい (現場の報告)。"""
        root = make_tool(self.work_root, "CoilCalculator", exe="CoilCalculator.exe",
                         app_id="nlm.coil-calculator", vbs=VBS_ARGS, entries=True,
                         server={"port": 8741, "port_retry": 5})
        text = self.hints(root / "Start.vbs", start_args="--no-browser")
        self.assertIn("ランチャー専用の窓 (アドレスバーの無いブラウザー) に開きます", text)
        self.assertIn("exe 版ではありません", text)
        self.assertNotIn("ランチャー専用の窓", self.hints(root / "Start.vbs"))


class CoilPackingTests(HintTestCase):
    """coil-packing-tools: 入口なし。exe は窓の ×、ブラウザー版は stop.bat。"""

    def setUp(self) -> None:
        super().setUp()
        self.root = make_tool(self.work_root, "コイル梱包ツール", exe="コイル梱包ツール.exe",
                              app_id="nlm.coil-packing-tools",
                              server={"port_retry": 3, "roles": {"main": {"port": 8740}}})

    def test_exe版は窓に頼むことを言う(self) -> None:
        text = self.hints(self.root / "コイル梱包ツール.exe", port=0)
        self.assertIn("窓の × を押したのと同じように閉じます", text)

    def test_exe版でstop_batを選ぶと自動を勧める(self) -> None:
        text = self.hints(self.root / "コイル梱包ツール.exe", port=0, stop_method="stop_bat")
        self.assertIn("停止方法は「自動」を勧めます", text)

    def test_ポートがappjsonと違えば言う(self) -> None:
        text = self.hints(self.root / "Start.vbs", port=8750)
        self.assertIn("ポートは 8740 にしてください (config/app.json の番号。いまは 8750)", text)
        self.assertNotIn("ポートは 8740 に",
                         self.hints(self.root / "Start.vbs", port=8740))

    def test_起動ファイルが無ければ選ぶよう言う(self) -> None:
        tool = Tool(app_id="x", display_name="x")
        self.assertIn("起動ファイルを［参照］で選んでください",
                      tool_registry.setting_hints(tool)[0])


class EntryUrlTests(ManagerTestCase):
    """入口の答えに画面の URL があれば、それを開く (設定のポートが埋まっていたとき)。"""

    def test_入口が答えたURLで画面を開く(self) -> None:
        start = make_entry_tool(self.work_root, "coilcalc", ready_after=0.2)
        root = start.parent
        _script(root / "launcher_check.bat",
                '[ -f ready.txt ] && { echo "ブラウザ版が使えます(http://127.0.0.1:8742/)"; exit 0; }\n'
                '[ -f started.txt ] && exit 2\nexit 1\n')
        self.addCleanup(lambda: (root / "stop.txt").write_text("")
                        if root.exists() else None)
        tool_registry.save(Tool(app_id="nlm.coil-calculator", display_name="VC長さ",
                                port=8741, start_command=str(start),
                                start_args="--no-browser"))
        tool = tool_registry.get("nlm.coil-calculator")
        self.start(tool)
        self.assertEqual(self.manager.status.state, State.RUNNING, self.manager.status.detail)
        running = self.manager.running["nlm.coil-calculator"]
        self.assertEqual((running.url, running.port), ("http://127.0.0.1:8742/", 8742))
        (root / "stop.txt").write_text("")
        deadline = time.monotonic() + 3
        while (root / "pid.txt").exists() and time.monotonic() < deadline:
            time.sleep(0.05)


def _have_tk() -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:                         # noqa: BLE001
        return False


@unittest.skipUnless(_have_tk(), "tkinter と画面が要る")
class SettingsRowTests(HintTestCase):
    """［設定］の行にヒントが出て、値を変えると出し直す。"""

    def setUp(self) -> None:
        self.addCleanup(release_tk, self)
        super().setUp()
        import tkinter

        from launcher.ui import settings_dialog

        self.root_tk = tkinter.Tk()
        self.addCleanup(self.root_tk.destroy)
        self.module = settings_dialog
        self.folder = make_tool(
            self.work_root, "梱包資材総合ツール", exe="梱包資材総合ツール.exe",
            app_id="nlm.packaging-tool",
            server={"roles": {"field": {"port": 8713}, "material": {"port": 8723}}})

    def row(self, start: str, port: int):
        tool = Tool(app_id="nlm.packaging-tool", display_name="梱包資材総合ツール",
                    port=port, start_command=str(self.folder / start))
        row = self.module._ToolRow(self.root_tk, tool, is_new=False)
        self.root_tk.update()
        return row

    def test_行にヒントが出て値を変えると出し直す(self) -> None:
        row = self.row("梱包資材総合ツール.exe", 8713)
        self.assertIn("ヒント: ポートは空にしてください", row.hint_label.cget("text"))
        self.assertTrue(row.hint_label.winfo_ismapped())
        row.port_var.set("")
        row.refresh_hints()
        self.assertNotIn("ポートは空に", row.hint_label.cget("text"))
        row.path_var.set(str(self.folder / "start.bat"))
        row.refresh_hints()
        self.assertIn("同じフォルダーの Start.vbs を選んでください", row.hint_label.cget("text"))


if __name__ == "__main__":
    unittest.main()
