"""ツール管理マスタ (1.7.14)

PC ごとに「どのログインID・PC名に・どのツールを出すか」を前もってマスタ (SQLite)
に書いておくと、その PC のランチャーが起動したときに自分の行のツールだけを並べる。

* 指定した場所にマスタが無ければ作る
* マスタに書いた起動ファイルがこの端末に無ければ飛ばす (知らせない)
* 表示ツール以外は隠す (消さない)。行が無くなれば戻す
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fake_tool_support import free_port, make_tool_dir  # noqa: E402
from _isolation import LocalAreaTestCase  # noqa: E402

from launcher import app_config, distribution, fileprobe, tool_master, tool_registry  # noqa: E402
from launcher.tool_master import MasterPc, MasterTool  # noqa: E402

LOGIN, PC = "Xfa-Ngykonpo", "NLM-NGY-240638"


class MasterTestCase(LocalAreaTestCase):

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"USERNAME": LOGIN, "COMPUTERNAME": PC})
        patcher.start()
        self.addCleanup(patcher.stop)
        tool_registry.initialize()
        # 製品の既定値の試験用ツールは使わない (マスタだけで決まることを見る)
        for tool in tool_registry.all_tools(include_disabled=True):
            tool_registry.delete_tool(tool.app_id)
        self.shared = self.work_root / "share"
        tool_registry.set_pc_setting(tool_registry.TOOL_MASTER_KEY, str(self.shared))
        self.path = tool_master.path_for(str(self.shared))

    def tool_dir(self, app_id: str, name: str) -> str:
        root = make_tool_dir(self.work_root, app_id=app_id, port=free_port(),
                             display_name=name)
        return str(root / "start.bat")

    def add_tool(self, name: str, start: str) -> int:
        return tool_master.save_tool(self.path, MasterTool(0, name=name, start_command=start))

    def add_pc(self, tools: list[int], *, login: str = LOGIN, pc: str = PC,
               enabled: bool = True) -> int:
        return tool_master.save_pc(self.path, MasterPc(0, login_id=login, pc_name=pc,
                                                        tool_nos=tuple(tools),
                                                        enabled=enabled))

    def shown(self) -> list[str]:
        return [t.display_name for t in tool_registry.all_tools()]


class CreateTests(MasterTestCase):

    def test_無ければ作る(self) -> None:
        self.assertFalse(self.path.exists())
        result = tool_master.sync()
        self.assertTrue(result.created)
        self.assertTrue(self.path.is_file())
        self.assertEqual(self.path.name, "launcher_ツール管理マスタ.sqlite3")
        with sqlite3.connect(self.path) as conn:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"tools", "pcs", "pc_tools", "meta"} <= names)
        self.assertEqual(result.state, tool_master.NO_ROW)

    def test_場所を決めていなければ使わない(self) -> None:
        tool_registry.clear_pc_setting(tool_registry.TOOL_MASTER_KEY)
        self.assertEqual(tool_master.sync().state, tool_master.OFF)
        self.assertFalse(self.path.exists())

    def test_ファイルを指定すればそのファイル(self) -> None:
        self.assertEqual(tool_master.path_for("/srv/ライン/master.db").name, "master.db")
        self.assertEqual(tool_master.path_for("distribution"),
                         app_config.APP_ROOT / "distribution" / tool_master.FILE_NAME)

    def test_配布先フォルダの場所も使う(self) -> None:
        tool_registry.export_distribution()
        self.assertEqual(distribution.tool_master(), str(self.shared))
        tool_registry.clear_pc_setting(tool_registry.TOOL_MASTER_KEY)
        self.assertEqual(tool_master.configured(), (str(self.shared), "配布先フォルダ"))

    def test_配布先フォルダを作り直しても場所を失わない(self) -> None:
        tool_registry.export_distribution()
        tool_registry.clear_pc_setting(tool_registry.TOOL_MASTER_KEY)
        tool_registry.export_distribution()            # 受け取った端末で作り直した
        self.assertEqual(distribution.tool_master(), str(self.shared))

    def test_届かなければ前のまま(self) -> None:
        nippou = self.add_tool("日報", self.tool_dir("nlm.nippou", "日報"))
        self.add_pc([nippou])
        tool_master.sync()
        self.assertEqual(self.shown(), ["日報"])
        with mock.patch.object(fileprobe, "probe",
                               return_value=fileprobe.Probe(fileprobe.UNKNOWN, "応答なし")):
            result = tool_master.sync()
        self.assertEqual(result.state, tool_master.UNREACHABLE)
        self.assertEqual(self.shown(), ["日報"])


class MatchTests(MasterTestCase):

    def test_ログインIDとPC名の両方が合う行を選ぶ(self) -> None:
        a = self.add_tool("日報", self.tool_dir("nlm.nippou", "日報"))
        b = self.add_tool("看板", self.tool_dir("nlm.kanban", "看板"))
        c = self.add_tool("点検表", self.tool_dir("nlm.inspection", "点検表"))
        self.add_pc([a], login="", pc="")                  # 全端末の既定
        self.add_pc([b], login="", pc=PC)                  # この PC ならだれでも
        self.add_pc([c], login=LOGIN.upper(), pc=PC.lower())   # 大文字小文字は見ない
        self.add_pc([a, b], login="Other", pc=PC)
        master = tool_master.read(self.path)
        self.assertEqual(master.row_for(LOGIN, PC).tool_nos, (c,))
        self.assertEqual(master.row_for("Someone", PC).tool_nos, (b,))
        self.assertEqual(master.row_for("Someone", "OTHER-PC").tool_nos, (a,))

    def test_無効の行は使わない(self) -> None:
        a = self.add_tool("日報", self.tool_dir("nlm.nippou", "日報"))
        self.add_pc([a], enabled=False)
        self.assertEqual(tool_master.sync().state, tool_master.NO_ROW)

    def test_同じログインIDとPC名の行は2つ作れない(self) -> None:
        self.add_pc([])
        with self.assertRaises(ValueError) as caught:
            self.add_pc([], login=LOGIN.lower())
        self.assertIn("管理番号 1", str(caught.exception))


class ApplyTests(MasterTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.nippou = self.add_tool("日報", self.tool_dir("nlm.nippou", "日報"))
        self.kanban = self.add_tool("看板", self.tool_dir("nlm.kanban", "看板"))
        # この PC には無いツール (起動ファイルが無い)
        self.inspection = self.add_tool("点検表", r"C:\業務ツール\点検表\Start.vbs")
        # この PC で前から使っていたツール (マスタの行で隠れる)
        tool_registry.save(tool_registry.Tool(
            app_id="nlm.local", display_name="前からのツール",
            start_command=self.tool_dir("nlm.local", "前からのツール")))

    def test_この端末の行のツールだけ並べる(self) -> None:
        number = self.add_pc([self.kanban, self.nippou, self.inspection])
        result = tool_master.sync()
        self.assertEqual(result.state, tool_master.APPLIED)
        self.assertEqual(result.pc.pc_no, number)
        # 並びはマスタのツールの並び (日報・看板)。点検表は起動ファイルが無いので出さない
        self.assertEqual(self.shown(), ["日報", "看板"])
        self.assertEqual(result.skipped, ["点検表"])
        # 前からのツールは隠す (消さない)
        local = tool_registry.get("nlm.local")
        self.assertIsNotNone(local)
        self.assertFalse(local.enabled)
        self.assertEqual(result.hidden, ["前からのツール"])
        # ポートなどはツールの設定から読む (［＋ ツールを追加］と同じ)
        nippou = tool_registry.get("nlm.nippou")
        self.assertTrue(nippou.port)
        self.assertIn("管理番号", result.describe())

    def test_行が無くなれば隠したツールを戻す(self) -> None:
        number = self.add_pc([self.nippou])
        tool_master.sync()
        self.assertEqual(self.shown(), ["日報"])
        tool_master.delete_pc(self.path, number)
        result = tool_master.sync()
        self.assertEqual(result.state, tool_master.NO_ROW)
        self.assertTrue(result.changed)
        self.assertIn("前からのツール", self.shown())

    def test_この端末で直した値は残す(self) -> None:
        self.add_pc([self.nippou])
        tool_master.sync()
        tool_registry.save(replace(tool_registry.get("nlm.nippou"), port=8713))
        result = tool_master.sync()
        self.assertEqual(tool_registry.get("nlm.nippou").port, 8713)
        self.assertFalse(result.changed, "変わっていないのに書き直しました")

    def test_起動ファイルは候補を複数書ける(self) -> None:
        start = tool_registry.get("nlm.local").start_command
        both = self.add_tool("候補つき", r"C:\無い場所\start.bat;" + start)
        self.add_pc([both])
        tool_master.sync()
        self.assertEqual(self.shown(), ["候補つき"])

    def test_ツールを消せば行からも外れる(self) -> None:
        self.add_pc([self.nippou, self.kanban])
        tool_master.delete_tool(self.path, self.kanban)
        master = tool_master.read(self.path)
        self.assertEqual(master.pcs[0].tool_nos, (self.nippou,))

    def test_並びを変える(self) -> None:
        tool_master.move_tool(self.path, self.kanban, -1)
        names = [t.name for t in tool_master.read(self.path).tools]
        self.assertEqual(names[:2], ["看板", "日報"])

    def test_今の設定から取り込む(self) -> None:
        added = tool_master.import_tools(self.path, tool_registry.all_tools(include_disabled=True))
        self.assertEqual(added, ["前からのツール"])
        again = tool_master.import_tools(self.path, tool_registry.all_tools(include_disabled=True))
        self.assertEqual(again, [], "同じ起動ファイルを2回足しました")

    def test_起動ファイルの形を確かめる(self) -> None:
        with self.assertRaises(ValueError):
            tool_master.save_tool(self.path, MasterTool(0, name="x", start_command=""))
        with self.assertRaises(ValueError):
            tool_master.save_tool(self.path, MasterTool(0, name="x",
                                                        start_command=r"C:\x\readme.txt"))

    def test_隠したツールを覚えておく(self) -> None:
        self.add_pc([self.nippou])
        tool_master.sync()
        hidden = json.loads(tool_registry.get_pc_setting(tool_master.HIDDEN_KEY))
        self.assertIn("nlm.local", hidden)
        self.assertNotIn("nlm.nippou", hidden)


class CatalogTests(MasterTestCase):
    """1.8.0: マスタのツールが全 PC のツールの一覧 (［設定］の［ツール］がそのまま)。"""

    def test_行の無いPCにはマスタのツールを全部出す(self) -> None:
        self.add_tool("日報", self.tool_dir("nlm.nippou", "日報"))
        self.add_tool("看板", self.tool_dir("nlm.kanban", "看板"))
        self.add_tool("点検表", r"C:\業務ツール\点検表\Start.vbs")    # この PC に無い
        result = tool_master.sync()
        self.assertEqual(result.state, tool_master.NO_ROW)
        self.assertEqual(self.shown(), ["日報", "看板"])
        self.assertIn("全部出しています: 日報、看板", result.describe())

    def test_マスタに書いた細かい設定を使う(self) -> None:
        start = self.tool_dir("nlm.nippou", "日報")
        tool_master.save_tool(self.path, MasterTool(0, name="日報(現場)", start_command=start,
                                                    app_id="nlm.nippou", port=8799,
                                                    start_args="", stop_method="stop_bat",
                                                    ui_mode="app"))
        tool_master.sync()
        tool = tool_registry.get("nlm.nippou")
        self.assertEqual((tool.display_name, tool.port, tool.start_args, tool.stop_method,
                          tool.ui_mode), ("日報(現場)", 8799, "", "stop_bat", "app"))

    def test_マスタから消したツールはこの端末からも消す(self) -> None:
        a = self.add_tool("日報", self.tool_dir("nlm.nippou", "日報"))
        self.add_tool("看板", self.tool_dir("nlm.kanban", "看板"))
        tool_registry.save(tool_registry.Tool(
            app_id="nlm.local", display_name="この端末だけ",
            start_command=self.tool_dir("nlm.local", "この端末だけ")))
        tool_master.sync()
        self.assertEqual(sorted(self.shown()), ["この端末だけ", "日報", "看板"])
        tool_master.delete_tool(self.path, a)
        result = tool_master.sync()
        self.assertEqual(result.removed, ["日報"])
        self.assertIsNone(tool_registry.get("nlm.nippou"))
        # マスタに無かった、この端末だけのツールは残す
        self.assertIsNotNone(tool_registry.get("nlm.local"))

    def test_場所を変えても前のマスタのツールは消さない(self) -> None:
        self.add_tool("日報", self.tool_dir("nlm.nippou", "日報"))
        tool_master.sync()
        tool_registry.set_pc_setting(tool_registry.TOOL_MASTER_KEY,
                                     str(self.work_root / "other"))
        result = tool_master.sync()
        self.assertEqual(result.removed, [])
        self.assertIsNotNone(tool_registry.get("nlm.nippou"))

    def test_前の版のマスタも読めて列を足す(self) -> None:
        self.path.parent.mkdir(parents=True)
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE tools (tool_no INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL DEFAULT '', start_command TEXT NOT NULL DEFAULT '',
                    order_no INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '', updated_by TEXT NOT NULL DEFAULT '');
                CREATE TABLE pcs (pc_no INTEGER PRIMARY KEY AUTOINCREMENT,
                    login_id TEXT NOT NULL DEFAULT '', pc_name TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '', updated_by TEXT NOT NULL DEFAULT '');
                CREATE TABLE pc_tools (pc_no INTEGER NOT NULL, tool_no INTEGER NOT NULL,
                    PRIMARY KEY (pc_no, tool_no));
                INSERT INTO meta VALUES ('schema_version', '1');
            """)
            conn.execute("INSERT INTO tools(name, start_command, order_no) VALUES(?, ?, 10)",
                         ("日報", self.tool_dir("nlm.nippou", "日報")))
        master = tool_master.read(self.path)
        self.assertEqual((master.tools[0].app_id, master.tools[0].port), ("", None))
        tool_master.sync()
        self.assertEqual(self.shown(), ["日報"])
        tool_master.ensure(self.path)
        with sqlite3.connect(self.path) as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(tools)")}
            version = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        self.assertTrue({"app_id", "port", "start_args", "stop_method", "ui_mode"} <= columns)
        self.assertEqual(version, str(tool_master.SCHEMA_VERSION))

    def test_まとめて書く(self) -> None:
        old = self.add_tool("日報", self.tool_dir("nlm.nippou", "日報"))
        gone = self.add_tool("看板", self.tool_dir("nlm.kanban", "看板"))
        numbers = tool_master.write_catalog(
            self.path,
            [MasterTool(0, name="点検表", start_command=r"C:\x\Start.vbs", app_id="nlm.i"),
             MasterTool(old, name="日報", start_command=r"C:\x\start.bat")],
            deleted_tools=[gone],
            pcs=[MasterPc(0, login_id=LOGIN, pc_name=PC, tool_nos=(-1, old))],
            known_tools=[old, gone])
        master = tool_master.read(self.path)
        self.assertEqual([t.name for t in master.tools], ["点検表", "日報"])
        self.assertEqual(master.pcs[0].tool_nos, (numbers[0], old))
        with self.assertRaises(ValueError):
            tool_master.write_catalog(self.path, [], pcs=[MasterPc(0, login_id="a"),
                                                         MasterPc(0, login_id="A")])

    def test_起動ファイルの形(self) -> None:
        self.assertEqual(tool_master.check_start(r"C:\a\start.bat;\\srv\a\Start.vbs"), "")
        self.assertIn("絶対パス", tool_master.check_start(r"a\start.bat"))
        self.assertIn(".bat", tool_master.check_start(r"C:\a\readme.txt"))


class SharedFolderTests(MasterTestCase):
    """共有フォルダーに置いたツール。朝つながる前に起動しても、その日消さない。"""

    def test_置き場所に届かなければ前のまま出す(self) -> None:
        start = self.tool_dir("nlm.nippou", "日報")
        a = self.add_tool("日報", start)
        b = self.add_tool("看板", self.tool_dir("nlm.kanban", "看板"))
        self.add_pc([a, b])
        tool_master.sync()
        self.assertEqual(self.shown(), ["日報", "看板"])
        real = fileprobe.probe

        def offline(path, **kwargs):
            if str(path) == start:
                return fileprobe.Probe(fileprobe.UNKNOWN, "応答なし")
            return real(path, **kwargs)

        with mock.patch.object(fileprobe, "probe", side_effect=offline):
            result = tool_master.sync()
        self.assertEqual(self.shown(), ["日報", "看板"])
        self.assertEqual(result.unreachable, ["日報"])
        self.assertEqual(tool_registry.get("nlm.nippou").start_command, start)
        self.assertIn("前のまま出している", result.describe())

    def test_無いと確かめたら出さない(self) -> None:
        start = self.tool_dir("nlm.nippou", "日報")
        a = self.add_tool("日報", start)
        self.add_pc([a])
        tool_master.sync()
        with mock.patch.object(fileprobe, "probe",
                               return_value=fileprobe.Probe(fileprobe.MISSING)):
            result = tool_master.sync()
        self.assertEqual(self.shown(), [])
        self.assertEqual(result.skipped, ["日報"])


class DesktopTests(MasterTestCase):
    """1.8.1: デスクトップに置いたツール。ログインIDが違うとデスクトップの道が違う。"""

    def desktops(self, who: str):
        home = self.work_root / "Users" / who
        return mock.patch.multiple(tool_master, desktop_dir=lambda: str(home / "Desktop"),
                                   profile_dir=lambda: str(home))

    def put(self, who: str) -> str:
        root = make_tool_dir(self.work_root / "Users" / who / "Desktop", app_id="nlm.nippou",
                             port=free_port(), display_name="日報")
        return str(root / "start.bat")

    def test_デスクトップの中は書き換えてマスタに入れる(self) -> None:
        start = self.put("Aさん")
        with self.desktops("Aさん"):
            text = tool_master.portable(start)
            self.assertTrue(text.startswith(tool_master.DESKTOP_TOKEN), text)
            self.assertEqual(Path(tool_master.expand(text)), Path(start))
            # ほかの場所・もう書き換えたものはそのまま
            self.assertEqual(tool_master.portable(r"\\srv\a\start.bat"), r"\\srv\a\start.bat")
            self.assertEqual(tool_master.portable(text), text)
        with self.desktops("Aさん"):
            profile = str(self.work_root / "Users" / "Aさん" / "tools" / "start.bat")
            self.assertTrue(tool_master.portable(profile).startswith(tool_master.PROFILE_TOKEN))

    def test_ログインIDが違うPCでもそのPCのデスクトップで見つける(self) -> None:
        with self.desktops("Aさん"):
            tool = tool_registry.Tool(app_id="nlm.nippou", display_name="日報",
                                      start_command=self.put("Aさん"))
            tool_master.save_tool(self.path, tool_master.from_tool(tool))
        self.assertIn(tool_master.DESKTOP_TOKEN,
                      tool_master.read(self.path).tools[0].start_command)
        start_b = self.put("Bさん")
        with self.desktops("Bさん"):
            tool_master.sync()
        self.assertEqual(self.shown(), ["日報"])
        self.assertEqual(Path(tool_registry.get("nlm.nippou").start_command), Path(start_b))


class BootTests(MasterTestCase):
    """起動したときに読む。届かなくても起動は止めず、［詳細］で知らせる。"""

    def test_起動したとき反映する(self) -> None:
        import boot

        nippou = self.add_tool("日報", self.tool_dir("nlm.nippou", "日報"))
        self.add_pc([nippou])
        result = boot.BootResult(started=True)
        boot._load_settings(result)
        self.assertEqual(self.shown(), ["日報"])
        self.assertEqual(result.notices, [])

    def test_届かなければ詳細で知らせる(self) -> None:
        import boot

        result = boot.BootResult(started=True)
        with mock.patch.object(fileprobe, "probe",
                               return_value=fileprobe.Probe(fileprobe.UNKNOWN, "応答なし")):
            boot._load_settings(result)
        self.assertEqual(result.warnings, [], "ダイアログを出しています")
        self.assertIn("ツール管理マスタを読めませんでした", result.notices[0])


if __name__ == "__main__":
    unittest.main()
