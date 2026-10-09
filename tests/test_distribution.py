"""配布先フォルダ (`distribution/`) と管理者パスワード

1台を［設定］で整えて配布先フォルダを作り、ランチャーのフォルダーごと
配る。配った先では起動時に読み込む。**その端末にすでにあるデータが
優先**で、配布先フォルダから入るのは、まだ無いツールと空欄の起動
ファイルだけ。［設定］はパスワードで守る。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import REAL_DISTRIBUTION_FOLDER, LocalAreaTestCase  # noqa: E402

from app_manager import ToolManager  # noqa: E402
from launcher import admin_lock, app_config, distribution, health, tool_registry  # noqa: E402

NIPPOU = "nlm.nippou-tool"
CALENDAR = "nlm.line-calendar"
KANBAN = "nlm.kanban-system"
PACKAGING = "nlm.packaging-tool"
DEFAULT_IDS = [NIPPOU, CALENDAR, KANBAN, PACKAGING]

# 本番の計算回数。試験では軽くするので、差し替える前に控えておく
PRODUCTION_ITERATIONS = distribution.ITERATIONS


class _Base(LocalAreaTestCase):
    """ランチャーの**隣**にツールを置いた配布物を作る。

        業務ツール/
          ランチャー/    ← APP_ROOT
          日報/start.bat
          カレンダー/start.bat
    """

    def setUp(self) -> None:
        super().setUp()
        # パスワードの計算を軽くする。本番の重さは `ITERATIONS` の試験で見る
        patcher = mock.patch.object(distribution, "ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.parent = self.work_root / "業務ツール"
        self.launcher_root = self.parent / "ランチャー"
        self.launcher_root.mkdir(parents=True)
        self.nippou_bat = self._make_bat("日報")
        self.calendar_bat = self._make_bat("カレンダー")

        patcher = mock.patch.object(app_config, "APP_ROOT", self.launcher_root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make_bat(self, folder: str) -> Path:
        path = self.parent / folder / "start.bat"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("@echo off\n", encoding="cp932")
        return path

    def write_settings(self, tools: list, *, encoding: str = "utf-8",
                       **extra) -> None:
        """配布先フォルダの設定を置く (配布元で作ったものが届いた状態)。"""
        data = {"format": distribution.FORMAT, "tools": tools, **extra}
        target = distribution.settings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False), encoding=encoding)

    def read_settings(self) -> dict:
        return json.loads(distribution.settings_path().read_text(encoding="utf-8"))

    def set_local(self, app_id: str, **changes) -> None:
        """その端末の［設定］で直したことにする。"""
        tool = tool_registry.get(app_id)
        fields = {**tool.__dict__, **changes}
        tool_registry.save(tool_registry.Tool(**fields))

    def nippou(self, **fields) -> dict:
        return {"app_id": NIPPOU, **fields}


# ------------------------------------------------------------------
# フォルダーとファイル
# ------------------------------------------------------------------
class FolderTests(_Base):

    def test_本来の置き場所はランチャーのフォルダーの中(self) -> None:
        """**フォルダーごと運ばれる場所**に置く。端末ごとの領域には置かない。"""
        self.assertEqual(REAL_DISTRIBUTION_FOLDER(),
                         self.launcher_root / "distribution")

    def test_無くても普通に動く(self) -> None:
        self.assertFalse(distribution.exists())
        self.assertEqual(distribution.load(), (None, ""))
        self.assertEqual(distribution.tools(), [])
        self.assertFalse(distribution.has_password())
        tool_registry.initialize()
        self.assertIsNotNone(tool_registry.get(NIPPOU))
        self.assertIn("なし", distribution.describe())
        self.assertEqual(distribution.state_text(), "まだ作っていません")

    def test_作ると必要なものがそろう(self) -> None:
        tool_registry.initialize()
        distribution.set_password("abcd")
        tool_registry.export_distribution()

        names = sorted(p.name for p in distribution.folder().iterdir())
        self.assertEqual(names, ["README.txt", "password.json", "settings.json"])
        self.assertTrue(distribution.exists())
        self.assertIn("ツール 4件", distribution.describe())
        self.assertIn("パスワード: あり", distribution.describe())

    def test_説明はメモ帳で読める形(self) -> None:
        """BOM 付き UTF-8・CRLF。古いメモ帳でも文字化けしない。"""
        tool_registry.export_distribution()
        raw = (distribution.folder() / "README.txt").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", raw)
        text = raw.decode("utf-8-sig")
        self.assertIn("password.json", text)
        self.assertIn("優先", text)

    def test_設定が壊れていても起動は止めない(self) -> None:
        self.write_settings([])
        distribution.settings_path().write_text('{"tools": [ // コメント\n]}',
                                                encoding="utf-8")
        data, problem = distribution.load()
        self.assertIsNone(data)
        self.assertIn("壊れています", problem)

        tool_registry.initialize()          # 例外にならない
        self.assertIsNotNone(tool_registry.get(NIPPOU))
        self.assertIn("[エラー]", distribution.describe())

    def test_壊れた設定は作り直せば直る(self) -> None:
        self.write_settings([])
        distribution.settings_path().write_text("{broken", encoding="utf-8")
        distribution.set_password("abcd")

        tool_registry.export_distribution()
        self.assertEqual(distribution.load()[1], "")
        self.assertTrue(distribution.verify_password("abcd"))

    def test_メモ帳で保存し直してBOMが付いても読める(self) -> None:
        self.write_settings([self.nippou(port=9001)], encoding="utf-8-sig")
        self.assertEqual(distribution.load()[1], "")
        self.assertEqual(distribution.tools()[0]["port"], 9001)

    def test_書き込みに失敗しても前の設定は残る(self) -> None:
        tool_registry.export_distribution()
        before = distribution.settings_path().read_text(encoding="utf-8")

        with mock.patch.object(distribution.os, "replace",
                               side_effect=OSError("ディスクがいっぱい")):
            with self.assertRaises(OSError):
                tool_registry.export_distribution()

        self.assertEqual(distribution.settings_path().read_text(encoding="utf-8"),
                         before)

    def test_読まれている最中なら少し待って差し替える(self) -> None:
        """共有フォルダーでは、ほかの端末が読んでいると一瞬だけ断られる。"""
        real = distribution.os.replace
        calls = []

        def busy_once(src, dst):
            calls.append(src)
            if len(calls) == 1:
                raise PermissionError("使用中")
            return real(src, dst)

        with mock.patch.object(distribution.os, "replace", side_effect=busy_once), \
                mock.patch.object(distribution, "REPLACE_WAIT_SEC", 0):
            tool_registry.export_distribution()
        self.assertEqual(len(calls), 2)
        self.assertTrue(distribution.tools())

    def test_差し替えられなければ一時ファイルを消す(self) -> None:
        with mock.patch.object(distribution.os, "replace",
                               side_effect=PermissionError("使用中")), \
                mock.patch.object(distribution, "REPLACE_WAIT_SEC", 0):
            with self.assertRaises(PermissionError):
                tool_registry.export_distribution()
        leftovers = [p.name for p in distribution.folder().iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_書き終えたら一時ファイルを残さない(self) -> None:
        tool_registry.export_distribution()
        distribution.set_password("abcd")
        names = sorted(p.name for p in distribution.folder().iterdir())
        self.assertEqual(names, ["README.txt", "password.json", "settings.json"])


# ------------------------------------------------------------------
# パスワード
# ------------------------------------------------------------------
class PasswordTests(_Base):

    def test_決めたパスワードでだけ開ける(self) -> None:
        distribution.set_password("line-2024")
        self.assertTrue(distribution.has_password())
        self.assertTrue(distribution.verify_password("line-2024"))
        self.assertFalse(distribution.verify_password("line-2025"))
        self.assertFalse(distribution.verify_password(""))
        self.assertFalse(distribution.verify_password("LINE-2024"))

    def test_無ければ誰も通さない(self) -> None:
        """「無い=誰でも」にしない。決める手順は `admin_lock` が持つ。"""
        self.assertFalse(distribution.verify_password(""))
        self.assertFalse(distribution.verify_password("anything"))

    def test_パスワードそのものは保存しない(self) -> None:
        distribution.set_password("himitsu-99")
        text = distribution.password_path().read_text(encoding="utf-8")
        self.assertNotIn("himitsu-99", text)
        record = json.loads(text)
        self.assertEqual(record["algorithm"], distribution.ALGORITHM)
        self.assertTrue(record["salt"])
        self.assertTrue(record["hash"])

    def test_パスワードは設定と別のファイル(self) -> None:
        """設定を作り直しても鍵が変わらず、忘れたら鍵だけ消せるように。"""
        distribution.set_password("abcd")
        tool_registry.export_distribution()
        settings = distribution.settings_path().read_text(encoding="utf-8")
        self.assertNotIn("password", settings)
        self.assertNotIn(json.loads(distribution.password_path()
                                    .read_text(encoding="utf-8"))["hash"], settings)

    def test_同じパスワードでも毎回ちがう形で残る(self) -> None:
        """塩を毎回変える。同じパスワードの現場どうしで見分けがつかないように。"""
        distribution.set_password("abcd")
        first = distribution.load_password()[0]
        distribution.set_password("abcd")
        second = distribution.load_password()[0]
        self.assertNotEqual(first["salt"], second["salt"])
        self.assertNotEqual(first["hash"], second["hash"])

    def test_短すぎる_前後に空白は断る(self) -> None:
        for text in ("", "abc", " abcd", "abcd "):
            with self.subTest(text=text), self.assertRaises(ValueError):
                distribution.set_password(text)
        self.assertFalse(distribution.password_path().exists())

    def test_壊れた記録では通さない(self) -> None:
        distribution.set_password("abcd")
        good = distribution.load_password()[0]
        for broken in ({**good, "salt": "zz"},
                       {**good, "algorithm": "md5"},
                       {**good, "iterations": "many"},
                       {"hash": "abcd"}):
            with self.subTest(broken=broken):
                distribution.password_path().write_text(json.dumps(broken),
                                                        encoding="utf-8")
                self.assertFalse(distribution.verify_password("abcd"))

    def test_本番の計算は重い(self) -> None:
        """ファイルを盗み見て総当たりされにくい重さにしておく。"""
        self.assertGreaterEqual(PRODUCTION_ITERATIONS, 100_000)

    def test_作り直してもパスワードは残る(self) -> None:
        distribution.set_password("abcd")
        tool_registry.export_distribution()
        tool_registry.export_distribution()
        self.assertTrue(distribution.verify_password("abcd"))

    def test_パスワードを変えてもツールの設定は変わらない(self) -> None:
        tool_registry.export_distribution()
        before = distribution.settings_path().read_text(encoding="utf-8")
        distribution.set_password("abcd")
        distribution.set_password("efgh")
        self.assertEqual(distribution.settings_path().read_text(encoding="utf-8"),
                         before)


# ------------------------------------------------------------------
# ［設定］を開く前の確認
# ------------------------------------------------------------------
class _Script:
    """画面の代わりに、決めておいた入力を順に返す。"""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []
        self.told: list[tuple[str, str]] = []

    def ask(self, title: str, message: str):
        self.asked.append(message)
        if not self.answers:
            raise AssertionError(f"想定より多く尋ねました: {message}")
        return self.answers.pop(0)

    def tell(self, kind: str, title: str, message: str) -> None:
        self.told.append((kind, message))

    @property
    def errors(self) -> list[str]:
        return [m for k, m in self.told if k == "error"]


def _pair(script: _Script):
    return script.ask, script.tell


class AdminLockTests(_Base):

    def unlock(self, script: _Script) -> bool:
        return admin_lock.unlock(script.ask, script.tell)

    def test_はじめてならその場で決める(self) -> None:
        script = _Script("line-01", "line-01")
        self.assertTrue(self.unlock(script))
        self.assertTrue(distribution.verify_password("line-01"))
        # 配布先フォルダが無くても、そこに作る
        self.assertTrue(distribution.password_path().exists())

    def test_決めずに閉じたら開かない(self) -> None:
        self.assertFalse(self.unlock(_Script(None)))
        self.assertFalse(distribution.exists())

    def test_確認が合わなければ決め直す(self) -> None:
        script = _Script("line-01", "line-02", "line-03", "line-03")
        self.assertTrue(self.unlock(script))
        self.assertTrue(distribution.verify_password("line-03"))
        self.assertFalse(distribution.verify_password("line-01"))
        self.assertTrue(any("一致しません" in m for m in script.errors))

    def test_短すぎれば決め直す(self) -> None:
        script = _Script("ab", "abcd", "abcd")
        self.assertTrue(self.unlock(script))
        self.assertTrue(any("4文字以上" in m for m in script.errors))

    def test_合っていれば開く(self) -> None:
        distribution.set_password("abcd")
        self.assertTrue(self.unlock(_Script("abcd")))

    def test_打ち間違いは直せる(self) -> None:
        distribution.set_password("abcd")
        script = _Script("abce", "abcd")
        self.assertTrue(self.unlock(script))
        self.assertEqual(len(script.errors), 1)

    def test_何度も違えば開かない(self) -> None:
        distribution.set_password("abcd")
        script = _Script(*["wrong"] * admin_lock.MAX_ATTEMPTS)
        self.assertFalse(self.unlock(script))
        self.assertEqual(len(script.asked), admin_lock.MAX_ATTEMPTS)
        self.assertIn("開きません", script.errors[-1])

    def test_取り消したら開かない(self) -> None:
        distribution.set_password("abcd")
        self.assertFalse(self.unlock(_Script(None)))

    def test_パスワードのファイルが読めなければ開かない(self) -> None:
        """確かめようがないので開かない。**尋ねもしない。**"""
        distribution.set_password("abcd")
        distribution.password_path().write_text("{broken", encoding="utf-8")
        script = _Script()
        self.assertFalse(self.unlock(script))
        self.assertEqual(script.asked, [])
        self.assertIn("password.json", script.errors[0])

    def test_設定が壊れていてもパスワードが読めれば開ける(self) -> None:
        """壊れた設定を作り直すには［設定］を開く必要がある。"""
        distribution.set_password("abcd")
        distribution.settings_path().write_text("{broken", encoding="utf-8")
        self.assertTrue(self.unlock(_Script("abcd")))

    def test_パスワードのファイルを消せば決め直せる(self) -> None:
        """忘れたときの戻し方。ツールの設定は消えない。"""
        distribution.set_password("abcd")
        tool_registry.export_distribution()
        distribution.password_path().unlink()

        self.assertTrue(self.unlock(_Script("efgh", "efgh")))
        self.assertTrue(distribution.verify_password("efgh"))
        self.assertTrue(distribution.tools())

    def test_保存できなければ決めたことにしない(self) -> None:
        """「決めました」と言って保存されていないと、次に開くとき
        「パスワードがありません」に戻ってしまう。"""
        script = _Script("abcd", "abcd")
        with mock.patch.object(distribution, "_write",
                               side_effect=PermissionError("読み取り専用")):
            self.assertFalse(self.unlock(script))
        self.assertFalse(distribution.has_password())
        self.assertIn("保存できません", script.errors[-1])

    def test_変えたら前のパスワードでは開かない(self) -> None:
        distribution.set_password("abcd")
        self.assertTrue(admin_lock.choose(*_pair(_Script("efgh", "efgh"))))
        self.assertFalse(distribution.verify_password("abcd"))
        self.assertTrue(distribution.verify_password("efgh"))


# ------------------------------------------------------------------
# 作る (配布元の端末)
# ------------------------------------------------------------------
class ExportTests(_Base):

    def setUp(self) -> None:
        super().setUp()
        tool_registry.initialize()
        tool_registry.set_start_command(NIPPOU, str(self.nippou_bat))
        tool_registry.set_start_command(CALENDAR, str(self.calendar_bat))

    def exported(self, app_id: str) -> dict:
        for item in self.read_settings()["tools"]:
            if item["app_id"] == app_id:
                return item
        self.fail(f"{app_id} が書き出されていません")

    def test_起動ファイルはランチャー起点の相対パスで書く(self) -> None:
        tool_registry.export_distribution()
        self.assertEqual(self.exported(NIPPOU)["start_command"],
                         "../日報/start.bat")
        self.assertTrue(distribution.is_portable("../日報/start.bat"))
        self.assertEqual(distribution.absolute_entries(), [])

    def test_別名の道から選んでも相対パスで書く(self) -> None:
        """ネットワークドライブ (Z:) やジャンクション越しに選んだ起動ファイル。

        ランチャーの場所は実体で持つので、選んだ道が別名のままだと
        「別のドライブ」と取り違えて相対にできない。ここではシンボリック
        リンクで同じ形を作る。
        """
        alias = self.work_root / "別名"
        try:
            alias.symlink_to(self.parent, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("シンボリックリンクを作れない環境です")
        tool_registry.set_start_command(NIPPOU, str(alias / "日報" / "start.bat"))
        tool_registry.export_distribution()
        self.assertEqual(self.exported(NIPPOU)["start_command"],
                         "../日報/start.bat")

    def test_区切りは斜線にする(self) -> None:
        """`\\` は JSON で2つ重ねる決まりがあり、手で直すと壊しやすい。"""
        tool_registry.export_distribution()
        text = distribution.settings_path().read_text(encoding="utf-8")
        self.assertNotIn("\\\\", text)

    def test_相対にしない書き方も選べる(self) -> None:
        tool_registry.export_distribution(relative=False)
        written = self.exported(NIPPOU)["start_command"]
        self.assertEqual(Path(written), self.nippou_bat)
        self.assertFalse(distribution.is_portable(written))
        self.assertTrue(any("日報" in e for e in distribution.absolute_entries()))

    def test_使わないツールも書き出す(self) -> None:
        """［使う］を外したことも配る。配った先でも隠れるように。"""
        self.set_local(CALENDAR, enabled=False)
        tool_registry.export_distribution()
        self.assertIs(self.exported(CALENDAR)["enabled"], False)

    def test_端末ごとの項目は書き出さない(self) -> None:
        self.set_local(NIPPOU, work_dir="C:/この端末だけ",
                       health_url_override="http://127.0.0.1:1/x")
        tool_registry.export_distribution()
        item = self.exported(NIPPOU)
        self.assertNotIn("work_dir", item)
        self.assertNotIn("health_url_override", item)
        self.assertEqual(set(item), set(distribution.EXPORT_FIELDS))

    def test_いつどこで作ったかを残す(self) -> None:
        tool_registry.export_distribution()
        data = self.read_settings()
        self.assertTrue(data["generated_at"])
        self.assertIn("generated_on", data)
        self.assertEqual(data["launcher_version"], app_config.version())
        self.assertIn("件", distribution.state_text())

    def test_作った端末の設定は変わらない(self) -> None:
        before = tool_registry.all_tools(include_disabled=True)
        tool_registry.export_distribution()
        tool_registry.initialize()          # 次の起動
        self.assertEqual(
            [(t.app_id, t.start_command, t.port, t.enabled)
             for t in tool_registry.all_tools(include_disabled=True)],
            [(t.app_id, t.start_command, t.port, t.enabled) for t in before])


# ------------------------------------------------------------------
# 起動時に読む (配った先の端末)
# ------------------------------------------------------------------
class LoadTests(_Base):

    def test_配った先で最初から設定済み(self) -> None:
        self.write_settings([
            self.nippou(display_name="日報(2ライン)", port=9001,
                        start_command="../日報/start.bat", start_args="",
                        stop_method="stop_bat"),
            {"app_id": CALENDAR, "enabled": False},
        ])
        tool_registry.initialize()

        tool = tool_registry.get(NIPPOU)
        self.assertEqual(tool.display_name, "日報(2ライン)")
        self.assertEqual(tool.port, 9001)
        self.assertEqual(tool.start_args, "")
        self.assertEqual(tool.stop_method, "stop_bat")
        self.assertEqual(Path(tool.start_command), self.nippou_bat.resolve())
        self.assertTrue(tool.is_configured)
        self.assertFalse(tool_registry.get(CALENDAR).enabled)

    def test_配布先フォルダにしか無いツールも入る(self) -> None:
        self._make_bat("検査")
        self.write_settings([{
            "app_id": "nlm.kensa-tool", "display_name": "検査",
            "order_no": 50, "port": 8800,
            "start_command": "../検査/start.bat"}])
        tool_registry.initialize()
        ids = [t.app_id for t in tool_registry.all_tools()]
        self.assertEqual(ids[-1], "nlm.kensa-tool")
        self.assertTrue(tool_registry.get("nlm.kensa-tool").is_configured)

    def test_既存データが優先(self) -> None:
        """すでに使っている端末に配布先フォルダを置いても、その端末の値は変わらない。"""
        tool_registry.initialize()
        self.set_local(NIPPOU, port=9100, display_name="日報(この端末)",
                       start_command=str(self.nippou_bat), enabled=False)

        self.write_settings([self.nippou(port=9001, display_name="日報(配布)",
                                         start_command="../カレンダー/start.bat",
                                         enabled=True)])
        tool_registry.initialize()          # 次の起動

        tool = tool_registry.get(NIPPOU)
        self.assertEqual(tool.port, 9100)
        self.assertEqual(tool.display_name, "日報(この端末)")
        self.assertEqual(tool.start_command, str(self.nippou_bat))
        self.assertFalse(tool.enabled)

    def test_作り直して配っても既存データが優先(self) -> None:
        self.write_settings([self.nippou(port=9001)])
        tool_registry.initialize()
        self.assertEqual(tool_registry.get(NIPPOU).port, 9001)
        self.set_local(NIPPOU, port=9100)

        self.write_settings([self.nippou(port=9002)])
        tool_registry.initialize()
        self.assertEqual(tool_registry.get(NIPPOU).port, 9100)

    def test_すでに使っている端末にも新しいツールは入る(self) -> None:
        """既存データに無いものは「まだデータが無い」。優先とぶつからない。"""
        tool_registry.initialize()
        self._make_bat("検査")
        self.write_settings([{"app_id": "nlm.kensa-tool", "display_name": "検査",
                              "port": 8800, "start_command": "../検査/start.bat"}])
        tool_registry.initialize()
        self.assertTrue(tool_registry.get("nlm.kensa-tool").is_configured)

    def test_空欄の起動ファイルは埋まる(self) -> None:
        tool_registry.initialize()
        self.assertEqual(tool_registry.get(NIPPOU).start_command, "")
        self.write_settings([self.nippou(start_command="../日報/start.bat")])
        tool_registry.initialize()
        self.assertTrue(tool_registry.get(NIPPOU).is_configured)

    def test_無い起動ファイルは入れない_あとで置けば埋まる(self) -> None:
        self.write_settings([{
            "app_id": "nlm.kensa-tool", "display_name": "検査", "port": 8800,
            "start_command": "../検査/start.bat"}])
        tool_registry.initialize()
        self.assertEqual(tool_registry.get("nlm.kensa-tool").start_command, "")

        self._make_bat("検査")                   # あとから置いた
        tool_registry.initialize()
        self.assertTrue(tool_registry.get("nlm.kensa-tool").is_configured)

    def test_その端末で消したツールは戻らない(self) -> None:
        self.write_settings([self.nippou(port=9001)])
        tool_registry.initialize()
        tool_registry.delete_tool(NIPPOU)
        tool_registry.initialize()
        self.assertIsNone(tool_registry.get(NIPPOU))

        self.write_settings([self.nippou(port=9002)])     # 作り直して配っても
        tool_registry.initialize()
        self.assertIsNone(tool_registry.get(NIPPOU))

    def test_形の合わない値は飛ばす(self) -> None:
        """1項目の誤りで起動が止まったり、ツールの設定が壊れたりしない。"""
        self.write_settings([
            {"app_id": "nlm.kensa-tool", "display_name": "", "port": "abc",
             "enabled": "yes", "stop_method": "kill", "order_no": True,
             "health_path": 3, "repository": "vba-kensa"},
        ])
        tool_registry.initialize()          # 例外にならない

        tool = tool_registry.get("nlm.kensa-tool")
        self.assertEqual(tool.display_name, "nlm.kensa-tool")
        self.assertEqual(tool.port, 0)
        self.assertTrue(tool.enabled)
        self.assertEqual(tool.stop_method, "auto")
        self.assertEqual(tool.order_no, 0)
        self.assertEqual(tool.health_path, tool_registry.DEFAULT_HEALTH_PATH)
        self.assertEqual(tool.repository, "vba-kensa")

    def test_使えないアプリIDは読まない(self) -> None:
        self.write_settings([{"app_id": "空白 のID", "display_name": "x"},
                             {"display_name": "IDなし"}, "文字だけ"])
        tool_registry.initialize()
        self.assertIsNone(tool_registry.get("空白 のID"))
        self.assertEqual(len(distribution.rejected()), 3)
        self.assertIn("[注意]", distribution.describe())

    def ids(self) -> list[str]:
        return [t.app_id for t in tool_registry.all_tools(include_disabled=True)]

    def test_配布元で消したツールは配った先に出ない(self) -> None:
        """配布先フォルダがあれば、その一覧が正。製品の既定値を混ぜない。"""
        self.write_settings([self.nippou(order_no=10),
                             {"app_id": CALENDAR, "order_no": 20}])
        tool_registry.initialize()
        self.assertEqual(self.ids(), [NIPPOU, CALENDAR])

    def test_一度起動したあとで配布先フォルダを置いてもそろう(self) -> None:
        """配布先フォルダを置く前に起動しただけの端末は「まだデータが無い」。"""
        tool_registry.initialize()                  # 工場出荷のまま
        self.assertEqual(self.ids(), DEFAULT_IDS)

        self.write_settings([
            self.nippou(display_name="日報(2ライン)", port=9001,
                        start_command="../日報/start.bat", order_no=10),
            {"app_id": CALENDAR, "enabled": False, "order_no": 20},
        ])
        tool_registry.initialize()                  # 次の起動

        self.assertEqual(self.ids(), [NIPPOU, CALENDAR])
        tool = tool_registry.get(NIPPOU)
        self.assertEqual((tool.display_name, tool.port), ("日報(2ライン)", 9001))
        self.assertTrue(tool.is_configured)
        self.assertFalse(tool_registry.get(CALENDAR).enabled)

    def test_そろえるのは最初の1回だけ(self) -> None:
        """そろえたあとは既存データ。作り直して配っても変わらない。"""
        tool_registry.initialize()
        self.write_settings([self.nippou(port=9001,
                                         start_command="../日報/start.bat")])
        tool_registry.initialize()
        self.write_settings([self.nippou(port=9002,
                                         start_command="../日報/start.bat")])
        tool_registry.initialize()
        self.assertEqual(tool_registry.get(NIPPOU).port, 9001)

    def test_手を入れた行はそろえない(self) -> None:
        """起動ファイルが空でも、表示名などを変えてあれば既存データ。"""
        tool_registry.initialize()
        self.set_local(NIPPOU, display_name="日報(この端末)")
        self.set_local(KANBAN, port=9999)
        self.write_settings([self.nippou(display_name="日報(配布)", port=9001)])
        tool_registry.initialize()

        tool = tool_registry.get(NIPPOU)
        self.assertEqual((tool.display_name, tool.port), ("日報(この端末)", 8733))
        # 配布先フォルダに無くても、手を入れた行は消さない
        self.assertEqual(tool_registry.get(KANBAN).port, 9999)

    def test_自分で足したツールは消さない(self) -> None:
        tool_registry.initialize()
        tool_registry.add_tool(tool_registry.Tool(
            app_id="site.own-tool", display_name="自作", port=8900))
        self.write_settings([self.nippou(port=9001)])
        tool_registry.initialize()
        self.assertIsNotNone(tool_registry.get("site.own-tool"))

    def test_配布先フォルダが設定を持たなければ既定値のまま(self) -> None:
        """パスワードだけ・壊れている・空のときは、既定値の行を消さない。"""
        for label, prepare in (
                ("パスワードだけ", lambda: distribution.set_password("abcd")),
                ("壊れている", lambda: distribution.settings_path()
                 .write_text("{broken", encoding="utf-8")),
                ("空", lambda: self.write_settings([]))):
            with self.subTest(label):
                tool_registry.initialize()
                distribution.folder().mkdir(parents=True, exist_ok=True)
                prepare()
                tool_registry.initialize()
                self.assertEqual(self.ids(), DEFAULT_IDS)

    def test_同じアプリIDが2度あれば1つにまとめる(self) -> None:
        self.write_settings([self.nippou(port=9001),
                             self.nippou(display_name="日報(後)")])
        self.assertEqual(len(distribution.tools()), 1)
        tool_registry.initialize()
        tool = tool_registry.get(NIPPOU)
        self.assertEqual((tool.port, tool.display_name), (9001, "日報(後)"))

    def test_診断で配布先フォルダの起動ファイルが見つからないことを出す(self) -> None:
        self.write_settings([self.nippou(start_command="../無い/start.bat")])
        text = tool_registry.describe()
        self.assertIn("../無い/start.bat", text)
        self.assertIn("見つかりません", text)


# ------------------------------------------------------------------
# 明示的に置き換える (その端末の［設定］から)
# ------------------------------------------------------------------
class ReloadTests(_Base):

    def setUp(self) -> None:
        super().setUp()
        self.write_settings([self.nippou(port=9001, display_name="日報(配布)",
                                         start_command="../日報/start.bat")])
        tool_registry.initialize()

    def test_置き換えると配布先フォルダの値になる(self) -> None:
        self.set_local(NIPPOU, port=9100, display_name="日報(この端末)")
        replaced = tool_registry.reload_from_distribution()
        self.assertEqual(replaced, [NIPPOU])
        tool = tool_registry.get(NIPPOU)
        self.assertEqual((tool.port, tool.display_name), (9001, "日報(配布)"))

    def test_消したツールも戻る(self) -> None:
        tool_registry.delete_tool(NIPPOU)
        tool_registry.reload_from_distribution()
        self.assertEqual(tool_registry.get(NIPPOU).port, 9001)

    def test_配布先フォルダに無いツールには触らない(self) -> None:
        tool_registry.save(tool_registry.Tool(
            app_id=CALENDAR, display_name="カレンダー", port=9300, enabled=False))
        tool_registry.reload_from_distribution()
        tool = tool_registry.get(CALENDAR)
        self.assertEqual((tool.port, tool.enabled), (9300, False))

    def test_書かれていない項目には触らない(self) -> None:
        self.set_local(NIPPOU, start_args="--local")
        tool_registry.reload_from_distribution()
        self.assertEqual(tool_registry.get(NIPPOU).start_args, "--local")

    def test_無い起動ファイルで端末の値を潰さない(self) -> None:
        self.write_settings([self.nippou(start_command="../無い/start.bat",
                                         port=9002)])
        tool_registry.reload_from_distribution()
        tool = tool_registry.get(NIPPOU)
        self.assertEqual(Path(tool.start_command), self.nippou_bat.resolve())
        self.assertEqual(tool.port, 9002)       # ほかの項目は入る

    def test_配布先フォルダが無ければ何もしない(self) -> None:
        shutil.rmtree(distribution.folder())
        self.set_local(NIPPOU, port=9100)
        self.assertEqual(tool_registry.reload_from_distribution(), [])
        self.assertEqual(tool_registry.get(NIPPOU).port, 9100)


# ------------------------------------------------------------------
# 通しで: 1台で整えて作る → フォルダーごと別の場所へ → 別の端末
# ------------------------------------------------------------------
class RoundTripTests(_Base):

    def setUp(self) -> None:
        super().setUp()
        # 配布先フォルダは本来の場所 (ランチャーのフォルダーの中) に置く。
        # フォルダーごと運ばれることを確かめたい
        patcher = mock.patch.object(distribution, "folder",
                                    REAL_DISTRIBUTION_FOLDER)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_配った先で同じ設定とパスワードになる(self) -> None:
        # --- 配布元の端末: 一度起動して［設定］で整え、配布先フォルダを作る ---
        self.assertTrue(admin_lock.unlock(*_pair(_Script("line-01", "line-01"))))
        tool_registry.initialize()
        tool_registry.set_start_command(NIPPOU, str(self.nippou_bat))
        tool_registry.set_start_command(CALENDAR, str(self.calendar_bat))
        self.set_local(NIPPOU, port=9001, display_name="日報(2ライン)")
        self.set_local(CALENDAR, enabled=False)
        tool_registry.export_distribution()
        self.assertTrue((self.launcher_root / "distribution"
                         / "settings.json").exists())

        # --- フォルダーごと別の場所 (別のドライブ名・別のフォルダー名) へ ---
        elsewhere = self.work_root / "別の端末" / "D_tools"
        shutil.copytree(self.parent, elsewhere)
        new_root = elsewhere / "ランチャー"

        # --- 配った先の端末 (設定DBはまっさら) ---
        other_local = self.local_root.parent / "other-local"
        with mock.patch.dict(os.environ,
                             {app_config.LOCAL_DIR_ENV: str(other_local)}), \
                mock.patch.object(app_config, "APP_ROOT", new_root):
            app_config.ensure_local_dirs()
            self.assertFalse(app_config.settings_db_path().exists())
            tool_registry.initialize()

            tool = tool_registry.get(NIPPOU)
            self.assertEqual(Path(tool.start_command),
                             (elsewhere / "日報" / "start.bat").resolve())
            self.assertEqual((tool.port, tool.display_name),
                             (9001, "日報(2ライン)"))
            self.assertTrue(tool.is_configured)
            self.assertFalse(tool_registry.get(CALENDAR).enabled)

            # ［設定］は同じパスワードで開く。決め直しを求めない
            script = _Script("line-01")
            self.assertTrue(admin_lock.unlock(script.ask, script.tell))
            self.assertEqual(len(script.asked), 1)

            # その端末で直した値は、次の起動でも残る (既存データが優先)
            self.set_local(NIPPOU, port=9500)
            tool_registry.initialize()
            self.assertEqual(tool_registry.get(NIPPOU).port, 9500)


# ------------------------------------------------------------------
# 通しで、実際に起動まで: 配布元で整える → フォルダーごと別の場所へ →
# まっさらな端末で、引き継いだ設定のままツールが起動する
# ------------------------------------------------------------------
from _fake_tool_support import free_port, make_tool_dir  # noqa: E402
from test_app_manager import ManagerTestCase  # noqa: E402


class InheritAndStartTests(ManagerTestCase):

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(distribution, "ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.parent = self.work_root / "業務ツール"
        self.launcher_root = self.parent / "ランチャー"
        self.launcher_root.mkdir(parents=True)
        patcher = mock.patch.object(app_config, "APP_ROOT", self.launcher_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 配布先フォルダは本来の場所 (ランチャーのフォルダーの中) に置く
        patcher = mock.patch.object(distribution, "folder",
                                    REAL_DISTRIBUTION_FOLDER)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_まっさらな端末で引き継いだ設定のまま起動できる(self) -> None:
        # --- 配布元の端末: 一度起動して［設定］で整える ---
        port = free_port()
        tool_dir = make_tool_dir(self.parent, app_id=NIPPOU, port=port,
                                 display_name="日報")
        self.assertTrue(admin_lock.unlock(
            *_pair(_Script("line-01", "line-01"))))
        tool_registry.save(tool_registry.Tool(
            app_id=NIPPOU, display_name="日報(2ライン)", port=port,
            start_command=str(tool_dir / "start.bat"),
            start_args="--no-browser", order_no=10))
        for app_id in (CALENDAR, KANBAN, PACKAGING):
            tool_registry.delete_tool(app_id)       # この現場では日報だけ
        tool_registry.export_distribution()

        # --- フォルダーごと別の場所へ ---
        elsewhere = self.work_root / "別の端末" / "D_tools"
        shutil.copytree(self.parent, elsewhere)

        # --- 配った先の端末: 設定DBはまっさら ---
        other_local = self.local_root.parent / "other-local"
        with mock.patch.dict(os.environ,
                             {app_config.LOCAL_DIR_ENV: str(other_local)}), \
                mock.patch.object(app_config, "APP_ROOT",
                                  elsewhere / "ランチャー"):
            app_config.ensure_local_dirs()
            self.assertFalse(app_config.settings_db_path().exists())
            tool_registry.initialize()

            tools = tool_registry.all_tools(include_disabled=True)
            self.assertEqual([t.app_id for t in tools], [NIPPOU])
            tool = tools[0]
            self.assertEqual(tool.display_name, "日報(2ライン)")
            self.assertEqual(Path(tool.start_command),
                             (elsewhere / NIPPOU / "start.bat").resolve())

            # 引き継いだ設定のまま、実際に起動する
            self.manager = ToolManager(on_status=self.statuses.append)
            try:
                self.start(tool)
                running = self.manager.current
                self.assertIsNotNone(running, self.statuses[-1:])
                self.assertEqual(running.app_id, NIPPOU)
                self.assertEqual(running.port, port)
                self.assertTrue(health.is_tool(health.probe(running.health_url), NIPPOU))
            finally:
                self._stop_everything()

            # ［設定］は同じパスワードで開く
            script = _Script("line-01")
            self.assertTrue(admin_lock.unlock(script.ask, script.tell))
            self.assertEqual(len(script.asked), 1)


class ActiveBarPositionTests(_Base):
    """ツールを起動したあとのバーの位置: その端末 → 配布先フォルダ → 既定。"""

    def test_既定は左下(self) -> None:
        self.assertEqual(tool_registry.active_bar_position(), "bottom_left")

    def test_配布先フォルダに書けば配った先もそうなる(self) -> None:
        tool_registry.set_active_bar_position("top_right")
        tool_registry.export_distribution()
        self.assertEqual(self.read_settings()["bar"],
                         {"position_idle": "center", "position_active": "top_right"})

        # 配った先 (その端末では決めていない)
        tool_registry.clear_pc_setting(tool_registry.BAR_POSITION_ACTIVE_KEY)
        self.assertEqual(tool_registry.active_bar_position(), "top_right")

    def test_その端末で決めたものが優先(self) -> None:
        self.write_settings([], bar={"position_active": "top_right"})
        tool_registry.set_active_bar_position("bottom_center")
        self.assertEqual(tool_registry.active_bar_position(), "bottom_center")

    def test_知らない値は使わない(self) -> None:
        self.write_settings([], bar={"position_active": "まんなか"})
        self.assertEqual(tool_registry.active_bar_position(), "bottom_left")
        with self.assertRaises(ValueError):
            tool_registry.set_active_bar_position("まんなか")
