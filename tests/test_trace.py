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
    """出来事の一覧 (launcher_出来事_<端末名>_YYYYMM.csv)。"""

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
        self.assertEqual(dest.path, app_config.local_dir("logs") / "launcher")
        self.assertEqual(dest.configured, "")
        self.assertIn("既定", dest.describe())

    def test_指定するとツールIDのフォルダーに書く(self) -> None:
        """現場のログの決まり (1.7.12): <出力先>\\launcher\\launcher_…。端末名を入れる。"""
        shared = self.work_root / "shared"
        tool_registry.set_pc_setting(tool_registry.LOG_DIR_KEY, str(shared))

        dest = trace.destination(refresh=True)
        self.assertEqual(dest.path, shared / "launcher")
        self.assertEqual(dest.source, "この端末の設定")
        trace.event("起動開始")
        written = shared / "launcher" / f"launcher_出来事_PC-01_{time.strftime('%Y%m')}.csv"
        self.assertTrue(written.exists())
        # 出力先の直下には何も置かない (端末名のフォルダーも作らない)
        self.assertEqual(sorted(p.name for p in shared.iterdir()), ["launcher"])

    def test_配布先フォルダの指定を使う(self) -> None:
        shared = self.work_root / "from-distribution"
        tool_registry.initialize()
        distribution.export_tools([], log_dir=str(shared))
        self.assertEqual(distribution.log_dir(), str(shared))

        dest = trace.destination(refresh=True)
        self.assertEqual(dest.path, shared / trace.TOOL_ID)
        self.assertEqual(dest.source, "配布先フォルダ")

    def test_端末の設定が配布先フォルダより優先(self) -> None:
        tool_registry.initialize()
        distribution.export_tools([], log_dir=str(self.work_root / "dist"))
        mine = self.work_root / "mine"
        tool_registry.set_pc_setting(tool_registry.LOG_DIR_KEY, str(mine))

        self.assertEqual(trace.destination(refresh=True).path, mine / trace.TOOL_ID)

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
        self.assertEqual(dest.path, app_config.local_dir("logs") / "launcher")
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
                self.assertEqual(dest.path, shared / trace.TOOL_ID)
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
        new_rows = trace.read_events(trace.events_path(shared / trace.TOOL_ID))
        self.assertEqual([r["種類"] for r in old_rows], ["ログの出力先を変更"])
        self.assertEqual(new_rows[0]["種類"], "ログの出力先を変更")
        self.assertEqual(new_rows[0]["結果"], "成功")

    def test_空にすると既定へ戻る(self) -> None:
        trace.set_log_dir(str(self.work_root / "shared"))
        dest = trace.set_log_dir("")
        self.assertEqual(dest.path, app_config.local_dir("logs") / "launcher")
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
        self.assertTrue(path.name.startswith("launcher_障害記録_"), path.name)

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
        """フォルダーはほかの端末と分け合う。名前の端末名で見分ける。前の場所も同じ。"""
        with mock.patch.dict(os.environ, {"COMPUTERNAME": "PC-01"}):
            shared = self.work_root / "shared"
            tool_registry.set_pc_setting(tool_registry.LOG_DIR_KEY, str(shared))
            folder = trace.destination(refresh=True).path
            mine = folder / "launcher_障害記録_PC-01_20260101_000000_日報_古い.txt"
            theirs = folder / "launcher_障害記録_PC-02_20260101_000000_日報_古い.txt"
            # 前の場所 (1.7.10 までの端末名のフォルダー・1.7.11 の日本語の名前)
            old_1710_mine = shared / "PC-01" / "incidents" / "古い.txt"
            old_1710_theirs = shared / "PC-02" / "incidents" / "古い.txt"
            old_1711 = shared / "業務ツール統合ランチャー"
            old_1711_mine = old_1711 / "業務ツール統合ランチャー_障害記録_PC-01_20260101_000000_a_b.txt"
            old_1711_theirs = old_1711 / "業務ツール統合ランチャー_障害記録_PC-02_20260101_000000_a_b.txt"
            # ほかのツールのフォルダー (決まり 5: 読まない・消さない)
            nippou = shared / "nippou" / "nippou_出来事_2026-01-01_PC-01.jsonl"
            root_file = shared / "launcher_障害記録_PC-01_20260101_000000_x_y.txt"
            past = time.time() - (trace.INCIDENT_KEEP_DAYS + 1) * 86400
            for path in (mine, theirs, old_1710_mine, old_1710_theirs, old_1711_mine,
                         old_1711_theirs, nippou, root_file):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
                os.utime(path, (past, past))

            trace.purge_old()
            logging_utils._purge_old_logs()
            for path in (mine, old_1710_mine, old_1711_mine):
                self.assertFalse(path.exists(), path)
            for path in (theirs, old_1710_theirs, old_1711_theirs):
                self.assertTrue(path.exists(), f"ほかの端末の記録を消しています: {path}")
            self.assertTrue(nippou.exists(), "ほかのツールの記録を消しています")
            self.assertTrue(root_file.exists(), "出力先の直下を片付けています")

    def test_新しい記録は前の場所の古い記録を消さない(self) -> None:
        """決まり 4: 前の場所の記録は移さない・消さない (残す期間までは残る)。"""
        with mock.patch.dict(os.environ, {"COMPUTERNAME": "PC-01"}):
            shared = self.work_root / "shared"
            tool_registry.set_pc_setting(tool_registry.LOG_DIR_KEY, str(shared))
            old = shared / "PC-01" / "events_202610.csv"
            old.parent.mkdir(parents=True)
            old.write_text("x", encoding="utf-8")
            trace.destination(refresh=True)
            trace.event("起動開始")
            trace.purge_old()
            self.assertEqual(old.read_text(encoding="utf-8"), "x")


class LogNameTests(LocalAreaTestCase):
    """現場のログの決まり (日報複合ツールに合わせる。1.7.12)。

    <出力先>\\launcher\\ に書き、名前は必ず launcher_ で始め、種類 (ふだんのログには
    付けない)・日付・端末名を入れる。ツールIDは半角英小文字。
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"COMPUTERNAME": "NLM-NGY-240638"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.shared = self.work_root / "shared"
        tool_registry.set_pc_setting(tool_registry.LOG_DIR_KEY, str(self.shared))
        self.folder = trace.destination(refresh=True).path

    def test_ツールIDは半角英小文字(self) -> None:
        self.assertRegex(trace.TOOL_ID, r"^[a-z]+$")
        self.assertEqual(self.folder, self.shared / "launcher")

    def test_書くファイルの名前(self) -> None:
        day = time.strftime("%Y%m%d")
        pc = "NLM-NGY-240638"
        self.assertEqual(trace.text_log_path(), self.folder / f"launcher_{pc}_{day}.log")
        self.assertEqual(logging_utils.log_file_path(), trace.text_log_path())
        self.assertEqual(trace.events_path().name, f"launcher_出来事_{pc}_{day[:6]}.csv")
        local = app_config.local_dir("logs") / "launcher"
        self.assertEqual(trace.tool_output_path("nlm.packaging-tool"),
                         local / f"launcher_ツール出力_nlm.packaging-tool_{pc}.log")
        self.assertEqual(trace.crash_log_path(), local / f"launcher_落ちたとき_{pc}.log")

    def test_片付けの名前の形はこの端末のものに当たる(self) -> None:
        import fnmatch

        patterns = logging_utils.log_patterns()
        mine = [trace.text_log_path().name, trace.tool_output_path("nlm.a").name,
                trace.tool_output_path("nlm.a").name + ".1"]
        for name in mine:
            self.assertTrue(any(fnmatch.fnmatch(name, p) for p in patterns), name)
        theirs = ["launcher_LINE-PC02_20261024.log",
                  "launcher_ツール出力_nlm.a_LINE-PC02.log", "nippou_動作記録_NLM-NGY-240638.log"]
        for name in theirs:
            self.assertFalse(any(fnmatch.fnmatch(name, p) for p in patterns), name)

    def test_障害記録も同じフォルダーに名前で分かるように(self) -> None:
        path = Path(trace.incident("起動できなかった", tool=tool_registry.Tool(
            app_id="nlm.nippou", display_name="日報")))
        self.assertEqual(path.parent, self.folder)
        self.assertRegex(path.name, r"^launcher_障害記録_NLM-NGY-240638_"
                                    r"\d{8}_\d{6}_日報_起動できなかった\.txt$")
        self.assertEqual(trace.recent_incidents(), [path])

    def test_ほかの端末の障害記録は詳細に出さない(self) -> None:
        other = self.folder / "launcher_障害記録_LINE-PC02_20261024_101010_日報_x.txt"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("x", encoding="utf-8")
        self.assertEqual(trace.recent_incidents(), [])

    def test_前の場所の最近の障害記録も読む(self) -> None:
        """入れ替えた直後でも、［詳細］から最近の記録が消えない (決まり 4)。"""
        root = app_config.local_dir("logs")
        olds = [self.shared / "NLM-NGY-240638" / "incidents" / "20261022_101010_日報_古い.txt",
                root / "incidents" / "20261022_101011_看板_古い.txt",
                self.shared / "業務ツール統合ランチャー"
                / "業務ツール統合ランチャー_障害記録_NLM-NGY-240638_20261023_1_a_b.txt",
                root / "業務ツール統合ランチャー_障害記録_NLM-NGY-240638_20261023_2_a_b.txt"]
        for path in olds:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        self.assertEqual(set(trace.recent_incidents()), set(olds))

    def test_前の版が落ちた記録も読む(self) -> None:
        old = app_config.local_dir("logs") / "launcher_crash.log"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text("--- 2026/10/24 08:00:00 ランチャー起動 pid=1 ---\n"
                       "Fatal Python error: Segmentation fault\n", encoding="utf-8")
        self.assertEqual(trace.crash_lines_since(time.time() - 60),
                         ["Fatal Python error: Segmentation fault"])

    def test_端末名に区切りの文字があってもほかの端末の記録を消さない(self) -> None:
        past = time.time() - (trace.INCIDENT_KEEP_DAYS + 1) * 86400
        with mock.patch.dict(os.environ, {"COMPUTERNAME": "LINE"}):
            other = self.folder / "launcher_障害記録_LINE-B_20260101_000000_x_y.txt"
            other.parent.mkdir(parents=True, exist_ok=True)
            other.write_text("x", encoding="utf-8")
            os.utime(other, (past, past))
            trace.purge_old()
            self.assertTrue(other.exists(), "ほかの端末の記録を消しています")
        with mock.patch.dict(os.environ, {"COMPUTERNAME": "LINE_B"}):
            self.assertEqual(trace.pc_tag(), "LINE-B")

    def test_設定の窓の説明もツールIDのフォルダー(self) -> None:
        self.assertEqual(trace.folder_for(str(self.shared)), self.shared / "launcher")


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
