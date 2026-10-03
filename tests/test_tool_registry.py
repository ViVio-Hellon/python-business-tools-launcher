"""ツール設定の保存と保存時チェック (要件定義書 §13 / §14 / §15)"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase  # noqa: E402

from launcher import app_config, tool_registry  # noqa: E402
from launcher.tool_registry import Tool  # noqa: E402


class SeedTests(LocalAreaTestCase):
    """初回起動で既定のツールが入る。"""

    def test_4つのツールが登録される(self) -> None:
        tool_registry.initialize()
        names = [t.display_name for t in tool_registry.all_tools()]
        self.assertEqual(names, ["日報", "カレンダー", "看板", "総合ツール"],
                         "並び順が要件定義書 §6 と違います")

    def test_startbatは既定では空(self) -> None:
        """端末ごとに違うので、もっともらしいパスを入れない。"""
        tool_registry.initialize()
        for tool in tool_registry.all_tools():
            with self.subTest(tool.display_name):
                self.assertEqual(tool.start_command, "")
                self.assertFalse(tool.is_configured)

    def test_利用者の変更を上書きしない(self) -> None:
        tool_registry.initialize()
        first = tool_registry.get("nlm.nippou-tool")
        tool_registry.save(replace(first, display_name="日報(改)",
                                   start_command="/tmp/x/start.bat"))

        tool_registry.initialize()
        again = tool_registry.get("nlm.nippou-tool")
        self.assertEqual(again.display_name, "日報(改)")
        self.assertEqual(again.start_command, "/tmp/x/start.bat")

    def test_消したツールは復活しない(self) -> None:
        """利用者が消したツールが、次の起動で戻ってこないこと。"""
        tool_registry.initialize()
        tool_registry.delete_tool("nlm.packaging-tool")

        tool_registry.initialize()
        self.assertIsNone(tool_registry.get("nlm.packaging-tool"))
        self.assertEqual(len(tool_registry.all_tools()), 3)

    def test_起動確認URLがポートから決まる(self) -> None:
        tool_registry.initialize()
        tool = tool_registry.get("nlm.nippou-tool")
        self.assertEqual(tool.health_url, "http://127.0.0.1:8733/api/health")
        self.assertEqual(tool.home_url, "http://127.0.0.1:8733/")

    def test_起動確認URLは差し替えられる(self) -> None:
        """要件定義書 §14「起動確認URL」を設定として持てること。"""
        tool_registry.initialize()
        tool = tool_registry.get("nlm.nippou-tool")
        tool_registry.save(replace(tool, health_url_override="http://127.0.0.1:9/health"))
        self.assertEqual(tool_registry.get("nlm.nippou-tool").health_url,
                         "http://127.0.0.1:9/health")


class GrowthTests(LocalAreaTestCase):
    """要件定義書 §14「将来5個目、6個目のツールを追加しやすい構造」。

    **ランチャー本体のコードを書き換えずに増やせること**を確かめる。
    増やす道は2つある ── 配布物の `config/launcher.json` に書き足す道と、
    端末の設定画面から足す道。
    """

    def test_配布物に書き足せば運用中の端末にも入る(self) -> None:
        """すでに使っている端末に、あとから5個目を届けられること。"""
        tool_registry.initialize()
        self.assertEqual(len(tool_registry.all_tools()), 4)

        # 配布物の config/launcher.json に5個目を足したことにする
        self.add_default(app_id="nlm.inspect", display_name="検査",
                         order_no=50, port=8750)

        tool_registry.initialize()          # 次回起動
        names = [t.display_name for t in tool_registry.all_tools()]
        self.assertEqual(names, ["日報", "カレンダー", "看板", "総合ツール", "検査"])

    def test_書き足しても既存の設定は残る(self) -> None:
        """**利用者が入れた start.bat のパスを上書きしない。**"""
        tool_registry.initialize()
        path = self.work_root / "start.bat"
        path.write_text("@echo off\n", encoding="utf-8")
        tool_registry.set_start_command("nlm.nippou-tool", str(path))

        self.add_default(app_id="nlm.inspect", display_name="検査", port=8750)
        tool_registry.initialize()

        self.assertEqual(tool_registry.get("nlm.nippou-tool").start_command,
                         str(path))

    def test_設定画面から足せる(self) -> None:
        tool_registry.initialize()
        tool_registry.add_tool(Tool(app_id="nlm.press", display_name="プレス",
                                    order_no=tool_registry.next_order_no(),
                                    port=8760))
        names = [t.display_name for t in tool_registry.all_tools()]
        self.assertEqual(names[-1], "プレス")
        self.assertEqual(tool_registry.get("nlm.press").port, 8760)

    def test_同じアプリIDは足せない(self) -> None:
        """アプリIDは起動確認の鍵。重複すると取り違える。"""
        tool_registry.initialize()
        with self.assertRaises(ValueError) as caught:
            tool_registry.add_tool(Tool(app_id="nlm.nippou-tool",
                                        display_name="日報2"))
        self.assertIn("すでに登録", str(caught.exception))

    def test_アプリIDの確かめ方(self) -> None:
        self.assertIn("入力", tool_registry.validate_app_id(""))
        self.assertIn("空白", tool_registry.validate_app_id("nlm press"))
        self.assertEqual(tool_registry.validate_app_id("nlm.press"), "")

    def test_並びの最後の番号を出せる(self) -> None:
        tool_registry.initialize()
        self.assertEqual(tool_registry.next_order_no(), 50)
        tool_registry.add_tool(Tool(app_id="nlm.x", display_name="X",
                                    order_no=tool_registry.next_order_no()))
        self.assertEqual(tool_registry.next_order_no(), 60)

    def add_default(self, **item) -> None:
        """同梱の既定値に1件足したことにする。"""
        defaults = app_config.load()["tools"]
        defaults.append(dict(item))
        self.addCleanup(defaults.remove, defaults[-1])


class ProbeTests(LocalAreaTestCase):
    """`start.bat` の隣から素性を読む。

    アプリIDを手で写させると、1文字違うだけで起動確認が永久に通らない。
    しかも画面には「応答がありません」としか出ないので、原因にたどり着き
    にくい。だから読み取る。
    """

    def make_tool(self, name: str, config: str):
        root = self.work_root / name
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "start.bat").write_text("@echo off\n", encoding="utf-8")
        (root / "config" / "app.json").write_text(config, encoding="utf-8")
        return root / "start.bat"

    def test_単一ポートのツール(self) -> None:
        start = self.make_tool("nippou",
            '{"app_id": "nlm.nippou-tool", "display_name": "日報管理ツール",'
            ' "server": {"host": "127.0.0.1", "port": 8733}}')
        self.assertEqual(tool_registry.probe_tool_folder(str(start)),
                         {"app_id": "nlm.nippou-tool",
                          "display_name": "日報管理ツール", "port": 8733})

    def test_役割を持つツールは最初の役割のポート(self) -> None:
        """看板や総合ツールのように、役割ごとにポートが分かれている場合。"""
        start = self.make_tool("kanban",
            '{"app_id": "nlm.kanban-system", "display_name": "資材発注看板",'
            ' "server": {"roles": {"site": {"port": 8741},'
            ' "warehouse": {"port": 8751}}}}')
        found = tool_registry.probe_tool_folder(str(start))
        self.assertEqual(found["port"], 8741)
        self.assertEqual(found["app_id"], "nlm.kanban-system")

    def test_読めなくても失敗しない(self) -> None:
        """設定が無いツールでも、手で入れれば登録できる。"""
        root = self.work_root / "unknown"
        root.mkdir(parents=True)
        (root / "start.bat").write_text("@echo off\n", encoding="utf-8")
        self.assertEqual(tool_registry.probe_tool_folder(str(root / "start.bat")), {})
        self.assertEqual(tool_registry.probe_tool_folder(""), {})

    def test_壊れた設定でも失敗しない(self) -> None:
        start = self.make_tool("broken", "{ これは JSON ではない")
        self.assertEqual(tool_registry.probe_tool_folder(str(start)), {})


class ValidationTests(LocalAreaTestCase):
    """要件定義書 §13.2 保存時チェック。"""

    def test_実在するbatは通る(self) -> None:
        path = self.work_root / "start.bat"
        path.write_text("@echo off\n", encoding="utf-8")
        self.assertEqual(tool_registry.validate_start_command(str(path)), "")

    def test_存在しないファイルは弾く(self) -> None:
        path = self.work_root / "ない.bat"
        problem = tool_registry.validate_start_command(str(path))
        self.assertIn("見つかりません", problem)

    def test_bat以外は弾く(self) -> None:
        path = self.work_root / "start.ps1"
        path.write_text("x", encoding="utf-8")
        problem = tool_registry.validate_start_command(str(path))
        self.assertIn(".bat", problem)

    def test_exeも指定できる(self) -> None:
        path = self.work_root / "業務アプリ.exe"
        path.write_bytes(b"MZ")
        self.assertEqual(tool_registry.validate_start_command(str(path)), "")

    def test_相対パスは弾く(self) -> None:
        """端末や起動元によって指す先が変わってしまうため。"""
        problem = tool_registry.validate_start_command("start.bat")
        self.assertIn("絶対パス", problem)

    def test_未設定は許す(self) -> None:
        """4つ全部を入れていない端末でもランチャーを使えるようにする。"""
        self.assertEqual(tool_registry.validate_start_command(""), "")
        self.assertEqual(tool_registry.validate_start_command("   "), "")

    def test_引用符を外して扱う(self) -> None:
        """エクスプローラの「パスのコピー」は引用符付きで入る。"""
        path = self.work_root / "start.bat"
        path.write_text("@echo off\n", encoding="utf-8")
        self.assertEqual(tool_registry.validate_start_command(f'"{path}"'), "")
        tool_registry.initialize()
        tool = tool_registry.set_start_command("nlm.nippou-tool", f'"{path}"')
        self.assertEqual(tool.start_command, str(path))

    def test_ポートの範囲を見る(self) -> None:
        self.assertEqual(tool_registry.validate_port(8733), "")
        self.assertEqual(tool_registry.validate_port(0), "")
        self.assertIn("65535", tool_registry.validate_port(70000))
        self.assertIn("65535", tool_registry.validate_port(-1))


class WorkDirTests(LocalAreaTestCase):
    """作業ディレクトリ (要件定義書 §14)。"""

    def test_未指定ならbatの置き場所(self) -> None:
        tool = Tool(app_id="x", display_name="日報",
                    start_command=str(self.work_root / "日報" / "start.bat"))
        self.assertEqual(tool.resolved_work_dir,
                         str(self.work_root / "日報"))

    def test_指定があればそちら(self) -> None:
        tool = Tool(app_id="x", display_name="日報",
                    start_command=str(self.work_root / "日報" / "start.bat"),
                    work_dir=str(self.work_root))
        self.assertEqual(tool.resolved_work_dir, str(self.work_root))


class ExtensibilityTests(LocalAreaTestCase):
    """要件定義書 §14「将来5個目、6個目のツールを追加しやすい構造」。"""

    def test_5個目を足せる(self) -> None:
        tool_registry.initialize()
        tool_registry.save(Tool(app_id="nlm.new-tool", display_name="新ツール",
                                order_no=50, port=8800))
        tools = tool_registry.all_tools()
        self.assertEqual(len(tools), 5)
        self.assertEqual(tools[-1].display_name, "新ツール")

    def test_使わないツールは隠せる(self) -> None:
        tool_registry.initialize()
        tool = tool_registry.get("nlm.packaging-tool")
        tool_registry.save(replace(tool, enabled=False))
        self.assertNotIn("総合ツール",
                         [t.display_name for t in tool_registry.all_tools()])
        self.assertIn("総合ツール",
                      [t.display_name
                       for t in tool_registry.all_tools(include_disabled=True)])


class PcSettingTests(LocalAreaTestCase):
    """要件定義書 §15 PCごとのモード設定。"""

    def test_モードを保存して読み直せる(self) -> None:
        tool_registry.initialize()
        self.assertEqual(tool_registry.pc_mode(), "")
        tool_registry.set_pc_mode("中板")
        self.assertEqual(tool_registry.pc_mode(), "中板")
        # 保存済みを使う。毎回選ばせない
        tool_registry.set_pc_mode("小板")
        self.assertEqual(tool_registry.pc_mode(), "小板")


class BackupTests(LocalAreaTestCase):
    """設定の控え (基盤仕様書 4.6)。"""

    def test_控えを取れる(self) -> None:
        tool_registry.initialize()
        backup = tool_registry.backup()
        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())
        self.assertEqual(backup.parent, app_config.local_dir("backup"))


if __name__ == "__main__":
    unittest.main()


class EntryKindTests(LocalAreaTestCase):
    """起動ファイルは .bat と .vbs のどちらでもよい。

    `.vbs` は**引数を転送するとは限らない**。4ツールの `Start.vbs` は
    `pythonw "<script>"` を決め打ちで実行しており、転送する処理が無い。
    届かないまま `--no-browser` を設定していると、ツールが自分で
    ブラウザーを開き、ランチャーも開いて**画面が2枚**になる。
    """

    def make(self, name: str, body: str = "", *, args: str = "--no-browser"):
        path = self.work_root / name
        path.write_text(body, encoding="cp932")
        return Tool(app_id="x", display_name="日報",
                    start_command=str(path), start_args=args)

    def test_batもvbsも登録できる(self) -> None:
        for name in ("start.bat", "Start.vbs"):
            with self.subTest(name):
                path = self.work_root / name
                path.write_text("", encoding="cp932")
                self.assertEqual(
                    tool_registry.validate_start_command(str(path)), "")

    def test_それ以外は弾く(self) -> None:
        path = self.work_root / "start.ps1"
        path.write_text("", encoding="cp932")
        problem = tool_registry.validate_start_command(str(path))
        self.assertIn(".bat", problem)
        self.assertIn(".vbs", problem)
        self.assertIn(".exe", problem)

    def test_exeはそのまま実行し引数も届く(self) -> None:
        tool = self.make("App.exe", "MZ")
        self.assertEqual(tool_registry.validate_start_command(tool.start_command), "")
        self.assertEqual(tool.entry_kind, "exe")
        self.assertTrue(tool.forwards_args)

    def test_batは引数を転送する扱い(self) -> None:
        tool = self.make("start.bat", "@echo off\npython start_app.py %*\n")
        self.assertEqual(tool.entry_kind, "bat")
        self.assertTrue(tool.forwards_args)
        self.assertTrue(tool.suppresses_browser)

    def test_転送しないvbsを見分ける(self) -> None:
        """4ツールの Start.vbs と同じ形。"""
        tool = self.make("Start.vbs",
                         'cmd = "pythonw " & Chr(34) & script & Chr(34)\n'
                         'shell.Run cmd, 0, False\n')
        self.assertEqual(tool.entry_kind, "vbs")
        self.assertFalse(tool.forwards_args)
        # **ランチャーは画面を開かない** (ツールが自分で開くため)
        self.assertFalse(tool.suppresses_browser)

    def test_転送するvbsなら届く(self) -> None:
        tool = self.make("Start.vbs",
                         'For i = 0 To WScript.Arguments.Count - 1\n'
                         '    args = args & " " & WScript.Arguments(i)\n'
                         'Next\n')
        self.assertTrue(tool.forwards_args)
        self.assertTrue(tool.suppresses_browser)

    def test_引数が無ければツールが画面を開く(self) -> None:
        tool = self.make("start.bat", "@echo off\n", args="")
        self.assertTrue(tool.forwards_args)
        self.assertFalse(tool.suppresses_browser,
                         "--no-browser が無いのにランチャーも画面を開きます")

    def test_読めないvbsは転送しない扱い(self) -> None:
        """**届くと思い込んで2枚開くより、届かない前提で1枚に寄せる。**"""
        tool = Tool(app_id="x", display_name="日報",
                    start_command=str(self.work_root / "無い.vbs"),
                    start_args="--no-browser")
        self.assertFalse(tool.forwards_args)

    def test_未設定なら種類は空(self) -> None:
        self.assertEqual(Tool(app_id="x", display_name="日報").entry_kind, "")


class DistributionTests(LocalAreaTestCase):
    """製品の既定値 (`config/launcher.json`) に書いた起動ファイル。

    **設定はランチャーのフォルダーと一緒に移動しない。** 各PCの
    `%LOCALAPPDATA%` にあるので、設定済みのフォルダーをコピーしても
    向こうでは全部「未設定」から始まる。既定値に書いておけば、配った
    先でも最初から埋まる (ふだんは画面から作る配布先フォルダ
    `distribution/` を使う。`test_distribution` を参照)。
    """

    def setUp(self) -> None:
        super().setUp()
        # 配布物の置き場所を作る。ランチャーの**隣**にツールを置く形
        self.parent = self.work_root / "業務ツール"
        self.launcher_root = self.parent / "ランチャー"
        self.launcher_root.mkdir(parents=True)
        tool_dir = self.parent / "日報"
        tool_dir.mkdir()
        self.start_bat = tool_dir / "start.bat"
        self.start_bat.write_text("@echo off\n", encoding="cp932")

        patcher = mock.patch.object(app_config, "APP_ROOT", self.launcher_root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, app_id: str, path: str) -> None:
        """既定値の既存ツールに起動ファイルを書いたことにする。"""
        for item in app_config.load()["tools"]:
            if item["app_id"] == app_id:
                original = item.get("start_command")
                item["start_command"] = path
                self.addCleanup(self._restore, item, original)
                return
        self.fail(f"{app_id} が既定値にありません")

    @staticmethod
    def _restore(item, original) -> None:
        if original is None:
            item.pop("start_command", None)
        else:
            item["start_command"] = original

    def test_ランチャー起点の相対パスで指せる(self) -> None:
        """**どこに置いても同じ書き方で指せる**こと。"""
        resolved = tool_registry.resolve_config_path(r"..\日報\start.bat".replace("\\", os.sep))
        self.assertEqual(Path(resolved), self.start_bat.resolve())

    def test_既定値の起動ファイルで最初から埋まる(self) -> None:
        self.configure("nlm.nippou-tool", os.path.join("..", "日報", "start.bat"))
        tool_registry.initialize()

        tool = tool_registry.get("nlm.nippou-tool")
        self.assertEqual(Path(tool.start_command), self.start_bat.resolve())
        self.assertTrue(tool.is_configured, "配った先で未設定のままです")

    def test_使っている端末でも空欄なら埋まる(self) -> None:
        """既定値をあとから更新しても、空欄なら届くこと。"""
        tool_registry.initialize()
        self.assertEqual(tool_registry.get("nlm.nippou-tool").start_command, "")

        self.configure("nlm.nippou-tool", os.path.join("..", "日報", "start.bat"))
        tool_registry.initialize()          # 次の起動

        self.assertTrue(tool_registry.get("nlm.nippou-tool").is_configured)

    def test_利用者が入れた値は上書きしない(self) -> None:
        """**空欄でなければ、それがその端末の正しい値。**"""
        other = self.work_root / "別の場所" / "start.bat"
        other.parent.mkdir()
        other.write_text("@echo off\n", encoding="cp932")
        tool_registry.initialize()
        tool_registry.set_start_command("nlm.nippou-tool", str(other))

        self.configure("nlm.nippou-tool", os.path.join("..", "日報", "start.bat"))
        tool_registry.initialize()

        self.assertEqual(tool_registry.get("nlm.nippou-tool").start_command,
                         str(other))

    def test_実在しないパスでは埋めない(self) -> None:
        """**空欄を「間違った値」に変えない。**

        入れてしまうと空欄ではなくなり、あとでツールを置いても
        自動では埋め直せない。
        """
        self.configure("nlm.nippou-tool", os.path.join("..", "無い", "start.bat"))
        tool_registry.initialize()
        self.assertEqual(tool_registry.get("nlm.nippou-tool").start_command, "")

    def test_あとからツールを置けば次の起動で埋まる(self) -> None:
        self.configure("nlm.line-calendar",
                       os.path.join("..", "カレンダー", "start.bat"))
        tool_registry.initialize()
        self.assertEqual(tool_registry.get("nlm.line-calendar").start_command, "")

        # あとでツールを配置した
        later = self.parent / "カレンダー" / "start.bat"
        later.parent.mkdir()
        later.write_text("@echo off\n", encoding="cp932")
        tool_registry.initialize()

        self.assertTrue(tool_registry.get("nlm.line-calendar").is_configured)

    def test_絶対パスもそのまま使える(self) -> None:
        self.configure("nlm.nippou-tool", str(self.start_bat))
        tool_registry.initialize()
        self.assertTrue(tool_registry.get("nlm.nippou-tool").is_configured)

    def test_環境変数を展開する(self) -> None:
        os.environ["BTL_TEST_ROOT"] = str(self.parent)
        self.addCleanup(os.environ.pop, "BTL_TEST_ROOT", None)
        text = os.path.join("%BTL_TEST_ROOT%" if os.name == "nt" else "$BTL_TEST_ROOT",
                            "日報", "start.bat")
        self.assertEqual(Path(tool_registry.resolve_config_path(text)),
                         self.start_bat.resolve())

    def test_見つからないときは診断に出す(self) -> None:
        """既定値にあるのに入っていない = 配置が想定と違う。"""
        self.configure("nlm.nippou-tool", os.path.join("..", "無い", "start.bat"))
        tool_registry.initialize()
        text = tool_registry.describe()
        self.assertIn("配布先フォルダ:", text)
        self.assertIn("見つかりません", text)


class OldDatabaseTests(LocalAreaTestCase):
    """古い版のランチャーが作った設定DBを、新しい版で開く。"""

    def test_足りない列を足して読める(self) -> None:
        import sqlite3

        path = app_config.settings_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            with conn:
                # 起動引数 (start_args) が無かったころの形
                conn.execute("""CREATE TABLE tools (
                    app_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    start_command TEXT NOT NULL DEFAULT '')""")
                conn.execute("INSERT INTO tools VALUES ('old.tool', '古いツール',"
                             " 'C:/old/start.bat')")
        finally:
            conn.close()

        tool_registry.initialize()
        tool = tool_registry.get("old.tool")
        self.assertEqual(tool.display_name, "古いツール")
        self.assertEqual(tool.start_command, "C:/old/start.bat")
        self.assertEqual(tool.start_args, "")
        self.assertEqual(tool.health_path, tool_registry.DEFAULT_HEALTH_PATH)
        self.assertTrue(tool.enabled)


class ShippedConfigTests(LocalAreaTestCase):
    """出荷する設定そのもの。"""

    def test_出荷時はツールを持たない(self) -> None:
        """現場ごとに使うツールが違う。［＋ ツールを追加］で登録する。"""
        import json

        shipped = json.loads(app_config.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(shipped.get("tools"), [])

        app_config.load()["tools"] = shipped["tools"]   # 試験用の4ツールを外す
        tool_registry.initialize()
        self.assertEqual(tool_registry.all_tools(include_disabled=True), [])

    def test_版を上げたら履歴にも書く(self) -> None:
        import launcher

        readme = (app_config.APP_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"| {launcher.__version__} |", readme,
                      "README の「版の履歴」に今の版がありません")



class RecommendStartArgsTests(LocalAreaTestCase):
    """起動引数が分からなくても、起動ファイルとツールの中身から決める。"""

    def make(self, entry: str, body: str, *, python: str = "") -> Path:
        folder = self.work_root / "tool"
        folder.mkdir(exist_ok=True)
        if python:
            (folder / "app.py").write_text(python, encoding="utf-8")
        path = folder / entry
        path.write_text(body, encoding="cp932")
        return path

    def test_引数を渡して受け付けるなら入れる(self) -> None:
        bat = self.make("start.bat", "@echo off\r\npython app.py %*\r\n",
                        python='parser.add_argument("--no-browser")\n')
        args, reason = tool_registry.recommend_start_args(str(bat))
        self.assertEqual(args, "--no-browser")
        self.assertIn("閉じます", reason)

    def test_起動ファイルが引数を渡さなければ空(self) -> None:
        bat = self.make("start.bat", "@echo off\r\npython app.py\r\n",
                        python='parser.add_argument("--no-browser")\n')
        args, reason = tool_registry.recommend_start_args(str(bat))
        self.assertEqual(args, "")
        self.assertIn("引数をツールへ渡さない", reason)

    def test_ツールが知らなければ空(self) -> None:
        """受け付けない引数を渡すと、起動そのものに失敗することがある。"""
        bat = self.make("start.bat", "@echo off\r\npython app.py %*\r\n",
                        python="print('hello')\n")
        args, reason = tool_registry.recommend_start_args(str(bat))
        self.assertEqual(args, "")
        self.assertIn("見つからない", reason)

    def test_VBSを選んだら隣のstartbatを勧める(self) -> None:
        self.make("start.bat", "@echo off\r\npython app.py %*\r\n",
                  python='"--no-browser"\n')
        vbs = self.make("Start.vbs", 'shell.Run "pythonw app.py", 0, False\r\n')
        args, reason = tool_registry.recommend_start_args(str(vbs))
        self.assertEqual(args, "")
        self.assertIn("start.bat を選ぶと", reason)

    def test_仮想環境の中までは探さない(self) -> None:
        """ライブラリの中の --no-browser を、ツールのものと取り違えない。"""
        bat = self.make("start.bat", "@echo off\r\npython app.py %*\r\n",
                        python="print('hello')\n")
        lib = bat.parent / ".venv" / "Lib" / "site-packages" / "other"
        lib.mkdir(parents=True)
        (lib / "cli.py").write_text('"--no-browser"\n', encoding="utf-8")
        self.assertEqual(tool_registry.recommend_start_args(str(bat))[0], "")

    def test_起動ファイルが無ければ判定しない(self) -> None:
        args, reason = tool_registry.recommend_start_args(
            str(self.work_root / "無い.bat"))
        self.assertEqual(args, "")
        self.assertIn("見つからない", reason)
