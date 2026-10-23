"""後追いの記録 (出来事の一覧・障害記録・ログの出力先)

「起動しなかった」をあとから追えるか。見るのは3つ:

* 出来事の一覧 … Excel で開ける (BOM)、1回の操作が操作IDでまとまる
* 障害記録     … なぜなぜの書式で、確かめた事実と見立てが分かれている
* 出力先       … 端末の設定 > 配布先フォルダ > 既定。書けなければ既定へ退く
"""
from __future__ import annotations

import logging
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase  # noqa: E402

from launcher import (admin_lock, app_config, distribution,  # noqa: E402
                      logging_utils, tool_registry, trace)


class _Tool:
    """記録に渡すツールの代わり (表示名・アプリID・ポートだけ)。"""

    def __init__(self, app_id: str = "nlm.daily", display_name: str = "日報",
                 port: int = 8733) -> None:
        self.app_id = app_id
        self.display_name = display_name
        self.port = port


class EventTests(LocalAreaTestCase):
    """出来事の一覧 (業務ツール統合ランチャー_出来事_<端末名>_YYYYMM.csv)。"""

    def test_Excelで開けるようBOM付きで書く(self) -> None:
        trace.event("起動開始", tool=_Tool())
        trace.event("起動完了", trace.OK, tool=_Tool(), elapsed=2.25)

        raw = trace.events_path().read_bytes()
        # **BOM は先頭に1つだけ。** 足すたびに付けると2行目から文字化けする
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.count(b"\xef\xbb\xbf"), 1)

        rows = trace.read_events()
        self.assertEqual(list(rows[0].keys()), list(trace.EVENT_FIELDS))
        self.assertEqual([r["種類"] for r in rows], ["起動開始", "起動完了"])
        self.assertEqual(rows[1]["結果"], "成功")
        self.assertEqual(rows[1]["経過秒"], "2.2")
        self.assertEqual(rows[1]["アプリID"], "nlm.daily")
        self.assertEqual(rows[1]["ポート"], "8733")
        self.assertEqual(rows[1]["ランチャー版"], app_config.version())

    def test_1回の操作は同じ操作IDでまとまる(self) -> None:
        first = trace.operation("ボタン", _Tool())
        second = trace.operation("ボタン", _Tool())
        trace.event("起動開始", tool=_Tool(), op=first)
        trace.event("起動開始", tool=_Tool(), op=second)
        trace.event("起動失敗", trace.FAILED, tool=_Tool(), op=first,
                    cause="ポートで待ち受けていない")

        rows = trace.read_events()
        ids = [r["操作ID"] for r in rows]
        self.assertNotEqual(first.op_id, second.op_id)
        self.assertEqual(ids, [first.op_id, second.op_id, first.op_id])
        # 操作の流れにも残る (障害記録の「直前の操作の流れ」になる)
        self.assertEqual(len(first.trail), 2)
        self.assertIn("ポートで待ち受けていない", first.trail[-1][1])

    def test_詳細の改行は1行にまとめる(self) -> None:
        """Excel で1件が複数行に割れると、絞り込みで切れる。"""
        trace.event("設定変更", detail="日報: ポート「1」→「2」\n\n看板: 削除")
        self.assertEqual(trace.read_events()[0]["詳細"],
                         "日報: ポート「1」→「2」 / 看板: 削除")

    def test_Excelで開いたままでも記録を失わない(self) -> None:
        """共有フォルダーの CSV を誰かが開いていると書けない。端末の中へ逃がす。"""
        shared = self.work_root / "shared"
        tool_registry.set_pc_setting(tool_registry.LOG_DIR_KEY, str(shared))
        trace.destination(refresh=True)
        # 書けない状態を作る (Excel のロックの代わりに、同じ名前のフォルダー)
        trace.events_path().mkdir(parents=True)

        trace.event("起動開始", tool=_Tool())

        local_rows = trace.read_events(trace.events_path(trace.local_dir()))
        self.assertEqual([r["種類"] for r in local_rows], ["起動開始"])

    def test_書けなくても例外を出さない(self) -> None:
        with mock.patch.object(trace, "_append_row", side_effect=OSError("x")):
            trace.event("起動開始")                 # 落ちなければよい


class DestinationTests(LocalAreaTestCase):
    """ログの出力先。端末の設定 > 配布先フォルダ > 既定。"""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"COMPUTERNAME": "PC-01"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_指定が無ければ端末の中(self) -> None:
        dest = trace.destination(refresh=True)
        self.assertEqual(dest.path, app_config.local_dir("logs"))
        self.assertEqual(dest.configured, "")
        self.assertIn("既定", dest.describe())

    def test_指定するとツール名のフォルダーに書く(self) -> None:
        """(1.7.11) 共有の出力先にはいろいろなツールが書く。フォルダーも名前も
        ツールの名前で始め、名前に端末名を入れる。"""
        shared = self.work_root / "shared"
        tool_registry.set_pc_setting(tool_registry.LOG_DIR_KEY, str(shared))

        dest = trace.destination(refresh=True)
        self.assertEqual(dest.path, shared / "業務ツール統合ランチャー")
        self.assertEqual(dest.source, "この端末の設定")
        trace.event("起動開始")
        written = shared / "業務ツール統合ランチャー" / \
            f"業務ツール統合ランチャー_出来事_PC-01_{time.strftime('%Y%m')}.csv"
        self.assertTrue(written.exists())
        self.assertFalse((shared / "PC-01").exists(), "端末名のフォルダーを作っています")

    def test_配布先フォルダの指定を使う(self) -> None:
        shared = self.work_root / "from-distribution"
        tool_registry.initialize()
        distribution.export_tools([], log_dir=str(shared))
        self.assertEqual(distribution.log_dir(), str(shared))

        dest = trace.destination(refresh=True)
        self.assertEqual(dest.path, shared / trace.LOG_NAME)
        self.assertEqual(dest.source, "配布先フォルダ")

    def test_端末の設定が配布先フォルダより優先(self) -> None:
        tool_registry.initialize()
        distribution.export_tools([], log_dir=str(self.work_root / "dist"))
        mine = self.work_root / "mine"
        tool_registry.set_pc_setting(tool_registry.LOG_DIR_KEY, str(mine))

        self.assertEqual(trace.destination(refresh=True).path, mine / trace.LOG_NAME)

    def test_配布先フォルダを作ると出力先も入る(self) -> None:
        tool_registry.initialize()
        tool_registry.set_pc_setting(tool_registry.LOG_DIR_KEY, r"\\server\logs")
        tool_registry.export_distribution()
        self.assertEqual(distribution.log_dir(), r"\\server\logs")

    def test_書けなければ既定の場所へ退く(self) -> None:
        """**記録のためにランチャーを止めない。** 退いた理由は残す。"""
        blocker = self.work_root / "not-a-folder"
        blocker.write_text("file", encoding="utf-8")
        tool_registry.set_pc_setting(tool_registry.LOG_DIR_KEY, str(blocker))

        dest = trace.destination(refresh=True)
        self.assertEqual(dest.path, app_config.local_dir("logs"))
        self.assertTrue(dest.problem)
        self.assertIn("退避中", dest.describe())
        trace.event("起動開始")                     # 退いた先に書ける
        self.assertTrue(trace.events_path().exists())

    def test_環境変数と相対パスを解く(self) -> None:
        with mock.patch.dict(os.environ, {"LAUNCHER_TEST_LOGS": str(self.work_root)}):
            self.assertEqual(trace.expand_dir("${LAUNCHER_TEST_LOGS}/logs"),
                             self.work_root / "logs")
        self.assertEqual(trace.expand_dir("logs"), app_config.APP_ROOT / "logs")

    def test_設定DBを作らずに読む(self) -> None:
        """出力先は起動のいちばん最初に要る。設定DBの用意より前。"""
        db = app_config.settings_db_path()
        self.assertFalse(db.exists())
        self.assertEqual(tool_registry.peek_pc_setting(tool_registry.LOG_DIR_KEY), "")
        self.assertFalse(db.exists(), "読むだけで設定DBを作っています")

    def test_出力先を変えるとその場で書き先が移る(self) -> None:
        shared = self.work_root / "shared"
        root = logging.getLogger(logging_utils.ROOT_NAME)
        before_handlers = list(root.handlers)
        with mock.patch.object(logging_utils, "_configured", True), \
                mock.patch.object(logging_utils, "_file_handler", None):
            dest = trace.set_log_dir(str(shared))
            try:
                # 試験では logging を設定していないので、既定で出る WARNING で
                logging_utils.get_logger("test").warning("移したあとの1行")
                self.assertEqual(dest.path, shared / trace.LOG_NAME)
                self.assertEqual(
                    tool_registry.get_pc_setting(tool_registry.LOG_DIR_KEY), str(shared))
                text = logging_utils.log_file_path().read_text(encoding="utf-8")
                self.assertIn("移したあとの1行", text)
            finally:
                for handler in list(root.handlers):
                    if handler not in before_handlers:
                        root.removeHandler(handler)
                        handler.close()

        # 変えたことは**移す前と後の両方**に残る
        old_rows = trace.read_events(trace.events_path(trace.local_dir()))
        new_rows = trace.read_events(trace.events_path(shared / trace.LOG_NAME))
        self.assertEqual([r["種類"] for r in old_rows], ["ログの出力先を変更"])
        self.assertEqual(new_rows[0]["種類"], "ログの出力先を変更")
        self.assertEqual(new_rows[0]["結果"], "成功")

    def test_空にすると既定へ戻る(self) -> None:
        trace.set_log_dir(str(self.work_root / "shared"))
        dest = trace.set_log_dir("")
        self.assertEqual(dest.path, app_config.local_dir("logs"))
        self.assertEqual(tool_registry.get_pc_setting(tool_registry.LOG_DIR_KEY), "")


class IncidentTests(LocalAreaTestCase):
    """障害記録 (なぜなぜ分析の下書き)。"""

    def test_なぜなぜの書式で書く(self) -> None:
        op = trace.operation("ボタン", _Tool())
        trace.event("起動開始", tool=_Tool(), op=op)
        out = self.work_root / "tool.out.log"
        out.write_text("準備中\nTraceback (most recent call last):\n"
                       "ModuleNotFoundError: No module named 'foo'\n",
                       encoding="utf-8")

        path = Path(trace.incident(
            "日報を起動できなかった", tool=_Tool(), op=op,
            whys=["起動確認から応答が無かった", "起動ファイルが終了した (戻り値 1)"],
            observed=[("ポート", "8733 は待ち受けていない")],
            hints=["start.bat をダブルクリックして、出るエラーを読む"],
            tool_log=out))

        raw = path.read_bytes()
        # メモ帳で開いて読めるように (BOM付き・CRLF)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", raw)
        text = raw.decode("utf-8-sig")
        self.assertIn("現象      : 日報を起動できなかった", text)
        self.assertIn(f"操作ID    : {op.op_id}", text)
        self.assertIn("なぜ1  起動確認から応答が無かった", text)
        self.assertIn("なぜ2  起動ファイルが終了した (戻り値 1)", text)
        # 人が書き足す欄を残す
        for blank in ("なぜ3", "なぜ4", "なぜ5", "根本原因", "対策"):
            self.assertIn(blank, text)
        # **見立ては事実と分ける**
        hint_at = text.index("■ 次に確かめること")
        self.assertGreater(hint_at, text.index("■ なぜなぜ"))
        self.assertIn("確かめてはいません", text)
        self.assertIn("■ 直前の操作の流れ", text)
        self.assertIn("ポート", text[text.index("■ ランチャーが見たこと"):])
        self.assertIn("| ModuleNotFoundError: No module named 'foo'", text)
        self.assertIn(str(trace.events_path()), text)
        self.assertIn(path, trace.recent_incidents())

    def test_例外の流れを残す(self) -> None:
        try:
            raise ValueError("試しの失敗")
        except ValueError as exc:
            path = trace.incident("想定外", exc=exc)
        text = Path(path).read_text(encoding="utf-8-sig")
        self.assertIn("■ 想定外の例外", text)
        self.assertIn("ValueError: 試しの失敗", text)
        self.assertIn("test_trace.py", text)

    def test_同じ想定外の失敗は1回だけ記録する(self) -> None:
        """見回りのように数秒ごとに回る処理で起きても、記録を量産しない。"""
        exc = RuntimeError("同じ失敗")
        first = trace.unexpected("生存監視", exc)
        second = trace.unexpected("生存監視", exc)
        other = trace.unexpected("生存監視", RuntimeError("別の失敗"))
        self.assertTrue(first)
        self.assertEqual(second, "")
        self.assertTrue(other)
        # 同じ秒・同じ題名でも、**前の記録を上書きしない**
        self.assertNotEqual(first, other)
        self.assertTrue(Path(first).exists())
        kinds = [r["種類"] for r in trace.read_events()]
        self.assertEqual(kinds.count("想定外の例外"), 2)

    def test_出力先に書けなければ端末の中に書く(self) -> None:
        shared = self.work_root / "shared"
        tool_registry.set_pc_setting(tool_registry.LOG_DIR_KEY, str(shared))
        dest = trace.destination(refresh=True)
        dest.path.rmdir()
        dest.path.write_text("ふさぐ", encoding="utf-8")   # フォルダーの代わりにファイル

        path = Path(trace.incident("試し"))
        self.assertEqual(path.parent, trace.local_dir())
        self.assertTrue(path.name.startswith("業務ツール統合ランチャー_障害記録_"), path.name)

    def test_ツールの出力の末尾とエラー行(self) -> None:
        out = self.work_root / "cp932.log"
        lines = [f"行{i}" for i in range(60)] + ["エラー: ファイルが見つかりません",
                                                  "終了します"]
        out.write_bytes("\r\n".join(lines).encode("cp932"))
        tail = trace.tail_lines(out, 5)
        self.assertEqual(tail[-1], "終了します")
        self.assertEqual(len(tail), 5)
        self.assertEqual(trace.error_line(tail), "エラー: ファイルが見つかりません")
        self.assertEqual(trace.tail_lines(self.work_root / "無い.log"), [])
        self.assertEqual(trace.error_line(["準備中", "起動しました"]), "")

    def test_古い記録は自分の端末のものだけ片付ける(self) -> None:
        """フォルダーはほかの端末と分け合う (1.7.11)。名前の端末名で見分ける。"""
        with mock.patch.dict(os.environ, {"COMPUTERNAME": "PC-01"}):
            shared = self.work_root / "shared"
            tool_registry.set_pc_setting(tool_registry.LOG_DIR_KEY, str(shared))
            folder = trace.destination(refresh=True).path
            folder.mkdir(parents=True, exist_ok=True)
            prefix = "業務ツール統合ランチャー_障害記録_"
            mine = folder / f"{prefix}PC-01_20260101_000000_日報_古い.txt"
            theirs = folder / f"{prefix}PC-02_20260101_000000_日報_古い.txt"
            # 1.7.10 までの置き場所
            legacy_mine = shared / "PC-01" / "incidents" / "古い.txt"
            legacy_theirs = shared / "PC-02" / "incidents" / "古い.txt"
            past = time.time() - (trace.INCIDENT_KEEP_DAYS + 1) * 86400
            for path in (mine, theirs, legacy_mine, legacy_theirs):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
                os.utime(path, (past, past))

            trace.purge_old()
            self.assertFalse(mine.exists())
            self.assertFalse(legacy_mine.exists())
            self.assertTrue(theirs.exists(), "ほかの端末の記録を消しています")
            self.assertTrue(legacy_theirs.exists(), "ほかの端末の記録を消しています")


class LogNameTests(LocalAreaTestCase):
    """(1.7.11) どのファイルも「業務ツール統合ランチャー_<種類>_」で始まり、端末名が入る。

    共有の出力先にはいろいろなツールが書く (dbviewer_…・rneconverter_…・出来事_…)。
    取り出したあとでも、開かずにどのツールの・どの端末の記録か分かるように。
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"COMPUTERNAME": "PC-01"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.shared = self.work_root / "shared"
        tool_registry.set_pc_setting(tool_registry.LOG_DIR_KEY, str(self.shared))
        self.folder = trace.destination(refresh=True).path

    def test_書くファイルの名前(self) -> None:
        day = time.strftime("%Y%m%d")
        self.assertEqual(trace.text_log_path(),
                         self.folder / f"業務ツール統合ランチャー_ログ_PC-01_{day}.log")
        self.assertEqual(logging_utils.log_file_path(), trace.text_log_path())
        self.assertEqual(trace.events_path().name,
                         f"業務ツール統合ランチャー_出来事_PC-01_{day[:6]}.csv")
        self.assertEqual(trace.tool_output_path("nlm.packaging-tool"),
                         trace.local_dir() / "業務ツール統合ランチャー_ツール出力_nlm.packaging-tool.log")
        self.assertEqual(trace.crash_log_path(),
                         trace.local_dir() / "業務ツール統合ランチャー_落ちたとき_PC-01.log")

    def test_障害記録も同じフォルダーに名前で分かるように(self) -> None:
        path = Path(trace.incident("起動できなかった", tool=tool_registry.Tool(
            app_id="nlm.nippou", display_name="日報")))
        self.assertEqual(path.parent, self.folder)
        self.assertRegex(path.name, r"^業務ツール統合ランチャー_障害記録_PC-01_"
                                    r"\d{8}_\d{6}_日報_起動できなかった\.txt$")
        self.assertEqual(trace.recent_incidents(), [path])
        # 「_」で区切ると、頭がツールの名前・次が種類 (ファイル名の一覧で並べたとき)
        self.assertEqual(path.name.split("_")[:3],
                         ["業務ツール統合ランチャー", "障害記録", "PC-01"])

    def test_以前の置き場所の最近の障害記録も読む(self) -> None:
        """入れ替えた直後でも、［詳細］から最近の記録が消えない。"""
        old = self.shared / "PC-01" / "incidents" / "20261022_101010_日報_古い.txt"
        old.parent.mkdir(parents=True)
        old.write_text("x", encoding="utf-8")
        local_old = trace.local_dir() / "incidents" / "20261022_101011_看板_古い.txt"
        local_old.parent.mkdir(parents=True, exist_ok=True)
        local_old.write_text("x", encoding="utf-8")
        self.assertEqual(set(trace.recent_incidents()), {old, local_old})

    def test_端末名に区切りの文字があってもほかの端末の記録を消さない(self) -> None:
        past = time.time() - (trace.INCIDENT_KEEP_DAYS + 1) * 86400
        with mock.patch.dict(os.environ, {"COMPUTERNAME": "LINE"}):
            other = self.folder / "業務ツール統合ランチャー_障害記録_LINE-B_20260101_000000_x_y.txt"
            other.parent.mkdir(parents=True, exist_ok=True)
            other.write_text("x", encoding="utf-8")
            os.utime(other, (past, past))
            trace.purge_old()
            self.assertTrue(other.exists(), "ほかの端末の記録を消しています")
        with mock.patch.dict(os.environ, {"COMPUTERNAME": "LINE_B"}):
            self.assertEqual(trace.pc_tag(), "LINE-B")

    def test_設定の窓の説明もツールの名前のフォルダー(self) -> None:
        self.assertEqual(trace.folder_for(str(self.shared)),
                         self.shared / "業務ツール統合ランチャー")


class AdminLockRecordTests(LocalAreaTestCase):
    """誰がいつ設定を開いたか (「いつから変わったか」を追う起点)。"""

    def test_パスワード違いと設定を開いたことを残す(self) -> None:
        distribution.set_password("abcd1234")
        answers = iter(["違う", "abcd1234"])
        self.assertTrue(admin_lock.unlock(lambda *_: next(answers),
                                          lambda *_: None))
        rows = [(r["種類"], r["結果"]) for r in trace.read_events()]
        self.assertEqual(rows, [("設定を開く", "注意"), ("設定を開く", "成功")])


class ChangeTests(unittest.TestCase):
    """設定の変わり目 (設定変更の記録に入る)。"""

    def test_変わったところだけ値つきで並べる(self) -> None:
        Tool = tool_registry.Tool
        before = [Tool(app_id="a", display_name="日報", port=1,
                       start_command="old.bat"),
                  Tool(app_id="b", display_name="看板")]
        after = [Tool(app_id="a", display_name="日報", port=2,
                      start_command="new.bat", enabled=False),
                 Tool(app_id="c", display_name="新しい")]
        lines = tool_registry.describe_changes(before, after)
        self.assertIn("日報: 起動ファイル 「old.bat」→「new.bat」", lines)
        self.assertIn("日報: ポート 「1」→「2」", lines)
        self.assertIn("日報: 使う 「はい」→「いいえ」", lines)
        self.assertIn("削除: 看板 (b)", lines)
        self.assertTrue(any(line.startswith("追加: 新しい (c)") for line in lines))
        self.assertEqual(tool_registry.describe_changes(before, before), [])


if __name__ == "__main__":
    unittest.main()
