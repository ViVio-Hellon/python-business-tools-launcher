"""配布設定 (`config/distribution.json`) と管理者パスワード

1台を［設定］で整えて書き出し、ランチャーのフォルダーごと配る。
配った先では次の起動で読み込まれる。［設定］はパスワードで守る。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import REAL_DISTRIBUTION_PATH, LocalAreaTestCase  # noqa: E402

from launcher import admin_lock, app_config, distribution, tool_registry  # noqa: E402

NIPPOU = "nlm.nippou-tool"
CALENDAR = "nlm.line-calendar"

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

    def write_distribution(self, tools: list[dict], **extra) -> None:
        data = {"format": distribution.FORMAT, "tools": tools, **extra}
        target = distribution.path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def read_distribution(self) -> dict:
        return json.loads(distribution.path().read_text(encoding="utf-8"))

    def set_local(self, app_id: str, **changes) -> None:
        """その端末の［設定］で直したことにする。"""
        tool = tool_registry.get(app_id)
        fields = {**tool.__dict__, **changes}
        tool_registry.save(tool_registry.Tool(**fields))


# ------------------------------------------------------------------
# ファイル
# ------------------------------------------------------------------
class FileTests(_Base):

    def test_本来の置き場所はランチャーのフォルダーのconfig(self) -> None:
        """**フォルダーごと運ばれる場所**に置く。端末ごとの領域には置かない。"""
        self.assertEqual(REAL_DISTRIBUTION_PATH(),
                         self.launcher_root / "config" / "distribution.json")

    def test_無くても普通に動く(self) -> None:
        self.assertFalse(distribution.path().exists())
        self.assertEqual(distribution.load(), (None, ""))
        self.assertEqual(distribution.tools(), [])
        self.assertEqual(distribution.tools_hash(), "")
        self.assertFalse(distribution.has_password())
        tool_registry.initialize()
        self.assertIsNotNone(tool_registry.get(NIPPOU))
        self.assertIn("なし", distribution.describe())

    def test_壊れていても起動は止めない(self) -> None:
        distribution.path().parent.mkdir(parents=True, exist_ok=True)
        distribution.path().write_text('{"tools": [ // コメント\n]}',
                                        encoding="utf-8")
        data, problem = distribution.load()
        self.assertIsNone(data)
        self.assertIn("壊れています", problem)

        tool_registry.initialize()          # 例外にならない
        self.assertIsNotNone(tool_registry.get(NIPPOU))
        self.assertIn("[エラー]", distribution.describe())

    def test_壊れた配布設定の上には書かない(self) -> None:
        """書くと、残っていたパスワードまで消える。"""
        distribution.path().parent.mkdir(parents=True, exist_ok=True)
        distribution.path().write_text("{broken", encoding="utf-8")

        with self.assertRaises(ValueError):
            distribution.set_password("abcd")
        with self.assertRaises(ValueError):
            tool_registry.export_distribution()
        self.assertEqual(distribution.path().read_text(encoding="utf-8"),
                         "{broken")

    def test_書き込みに失敗しても前の配布設定は残る(self) -> None:
        distribution.set_password("abcd")
        before = distribution.path().read_text(encoding="utf-8")

        with mock.patch.object(distribution.os, "replace",
                               side_effect=OSError("ディスクがいっぱい")):
            with self.assertRaises(OSError):
                tool_registry.export_distribution()

        self.assertEqual(distribution.path().read_text(encoding="utf-8"), before)
        self.assertTrue(distribution.verify_password("abcd"))

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
        self.assertEqual(list(distribution.path().parent.iterdir()), [])

    def test_書き終えたら一時ファイルを残さない(self) -> None:
        tool_registry.export_distribution()
        leftovers = [p.name for p in distribution.path().parent.iterdir()
                     if p.name != distribution.FILE_NAME]
        self.assertEqual(leftovers, [])


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
        """「無い=誰でも」にすると、ファイルを消すだけで外れる形が
        確かめる側にまで広がる。決める手順は `admin_lock` が持つ。"""
        self.assertFalse(distribution.verify_password(""))
        self.assertFalse(distribution.verify_password("anything"))

    def test_パスワードそのものは保存しない(self) -> None:
        distribution.set_password("himitsu-99")
        text = distribution.path().read_text(encoding="utf-8")
        self.assertNotIn("himitsu-99", text)
        record = self.read_distribution()["password"]
        self.assertEqual(record["algorithm"], distribution.ALGORITHM)
        self.assertTrue(record["salt"])
        self.assertTrue(record["hash"])

    def test_同じパスワードでも毎回ちがう形で残る(self) -> None:
        """塩を毎回変える。同じパスワードの現場どうしで見分けがつかないように。"""
        distribution.set_password("abcd")
        first = self.read_distribution()["password"]
        distribution.set_password("abcd")
        second = self.read_distribution()["password"]
        self.assertNotEqual(first["salt"], second["salt"])
        self.assertNotEqual(first["hash"], second["hash"])

    def test_短すぎる_前後に空白は断る(self) -> None:
        for text in ("", "abc", " abcd", "abcd "):
            with self.subTest(text=text), self.assertRaises(ValueError):
                distribution.set_password(text)
        self.assertFalse(distribution.path().exists())

    def test_壊れた記録では通さない(self) -> None:
        distribution.set_password("abcd")
        data = self.read_distribution()
        for broken in ({**data["password"], "salt": "zz"},
                       {**data["password"], "algorithm": "md5"},
                       {**data["password"], "iterations": "many"},
                       "abcd"):
            with self.subTest(broken=broken):
                self.write_distribution([], password=broken)
                self.assertFalse(distribution.verify_password("abcd"))

    def test_本番の計算は重い(self) -> None:
        """ファイルを盗み見て総当たりされにくい重さにしておく。"""
        self.assertGreaterEqual(PRODUCTION_ITERATIONS, 100_000)

    def test_パスワードを変えてもツール設定は反映し直さない(self) -> None:
        """変えるたびに全端末の設定が上書きし直されると困る。"""
        tool_registry.export_distribution()
        before = distribution.tools_hash()
        distribution.set_password("abcd")
        distribution.set_password("efgh")
        self.assertEqual(distribution.tools_hash(), before)

    def test_書き出してもパスワードは残る(self) -> None:
        distribution.set_password("abcd")
        tool_registry.export_distribution()
        tool_registry.export_distribution()
        self.assertTrue(distribution.verify_password("abcd"))


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


class AdminLockTests(_Base):

    def unlock(self, script: _Script) -> bool:
        return admin_lock.unlock(script.ask, script.tell)

    def test_はじめてならその場で決める(self) -> None:
        script = _Script("line-01", "line-01")
        self.assertTrue(self.unlock(script))
        self.assertTrue(distribution.verify_password("line-01"))

    def test_決めずに閉じたら開かない(self) -> None:
        self.assertFalse(self.unlock(_Script(None)))
        self.assertFalse(distribution.path().exists())

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

    def test_配布設定が読めなければ開かない(self) -> None:
        """確かめようがないので開かない。**尋ねもしない。**"""
        distribution.path().parent.mkdir(parents=True, exist_ok=True)
        distribution.path().write_text("{broken", encoding="utf-8")
        script = _Script()
        self.assertFalse(self.unlock(script))
        self.assertEqual(script.asked, [])
        self.assertIn("distribution.json", script.errors[0])

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
# 書き出す (管理者の端末)
# ------------------------------------------------------------------
class ExportTests(_Base):

    def setUp(self) -> None:
        super().setUp()
        tool_registry.initialize()
        tool_registry.set_start_command(NIPPOU, str(self.nippou_bat))
        tool_registry.set_start_command(CALENDAR, str(self.calendar_bat))

    def exported(self, app_id: str) -> dict:
        for item in self.read_distribution()["tools"]:
            if item["app_id"] == app_id:
                return item
        self.fail(f"{app_id} が書き出されていません")

    def test_起動ファイルはランチャー起点の相対パスで書く(self) -> None:
        tool_registry.export_distribution()
        self.assertEqual(self.exported(NIPPOU)["start_command"],
                         "../日報/start.bat")
        self.assertTrue(distribution.is_portable("../日報/start.bat"))
        self.assertEqual(distribution.absolute_entries(), [])

    def test_区切りは斜線にする(self) -> None:
        """`\\` は JSON で2つ重ねる決まりがあり、手で直すと壊しやすい。"""
        tool_registry.export_distribution()
        text = distribution.path().read_text(encoding="utf-8")
        self.assertNotIn("\\\\", text)

    def test_相対にしない書き方も選べる(self) -> None:
        tool_registry.export_distribution(relative=False)
        written = self.exported(NIPPOU)["start_command"]
        self.assertEqual(Path(written), self.nippou_bat)
        self.assertFalse(distribution.is_portable(written))
        self.assertTrue(any(NIPPOU in e or "日報" in e
                            for e in distribution.absolute_entries()))

    def test_使わないツールも書き出す(self) -> None:
        """［使う］を外したことも配る。配った先でも止まるように。"""
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

    def test_いつどこで書き出したかを残す(self) -> None:
        tool_registry.export_distribution()
        data = self.read_distribution()
        self.assertTrue(data["generated_at"])
        self.assertIn("generated_on", data)
        self.assertEqual(data["launcher_version"], app_config.version())
        self.assertIn("ツール:", distribution.describe())

    def test_書き出した端末では読み込み直さない(self) -> None:
        """自分の書き出しを読み込み直すと、入れたパスが書き換わり、
        ログにも「反映しました」が出て紛らわしい。"""
        before = tool_registry.get(NIPPOU).start_command
        tool_registry.export_distribution()
        with mock.patch.object(tool_registry, "log") as log:
            tool_registry.initialize()      # 次の起動
            after = tool_registry.get(NIPPOU).start_command
        applied = [c for c in log.info.call_args_list
                   if "配布設定を反映しました" in str(c)]
        self.assertEqual(applied, [])
        self.assertEqual(after, before)

        self.set_local(NIPPOU, port=9999)
        tool_registry.initialize()
        self.assertEqual(tool_registry.get(NIPPOU).port, 9999)


# ------------------------------------------------------------------
# 読み込む (配った先の端末)
# ------------------------------------------------------------------
class ApplyTests(_Base):

    def nippou(self, **fields) -> dict:
        return {"app_id": NIPPOU, **fields}

    def test_配った先で最初から設定済み(self) -> None:
        self.write_distribution([
            self.nippou(display_name="日報(2ライン)", port=9001,
                        start_command="../日報/start.bat", start_args=""),
            {"app_id": CALENDAR, "enabled": False},
        ])
        tool_registry.initialize()

        tool = tool_registry.get(NIPPOU)
        self.assertEqual(tool.display_name, "日報(2ライン)")
        self.assertEqual(tool.port, 9001)
        self.assertEqual(tool.start_args, "")
        self.assertEqual(Path(tool.start_command), self.nippou_bat.resolve())
        self.assertTrue(tool.is_configured)
        self.assertFalse(tool_registry.get(CALENDAR).enabled)

    def test_配布設定にしか無いツールも入る(self) -> None:
        self._make_bat("検査")
        self.write_distribution([{
            "app_id": "nlm.kensa-tool", "display_name": "検査",
            "order_no": 50, "port": 8800,
            "start_command": "../検査/start.bat"}])
        tool_registry.initialize()
        ids = [t.app_id for t in tool_registry.all_tools()]
        self.assertIn("nlm.kensa-tool", ids)
        self.assertEqual(ids[-1], "nlm.kensa-tool")
        self.assertTrue(tool_registry.get("nlm.kensa-tool").is_configured)

    def test_変わっていなければ端末で直した値を残す(self) -> None:
        self.write_distribution([self.nippou(port=9001)])
        tool_registry.initialize()
        self.set_local(NIPPOU, port=9100)
        tool_registry.initialize()          # 次の起動
        tool_registry.initialize()          # その次
        self.assertEqual(tool_registry.get(NIPPOU).port, 9100)

    def test_配り直すと端末の値より優先する(self) -> None:
        self.write_distribution([self.nippou(port=9001)])
        tool_registry.initialize()
        self.set_local(NIPPOU, port=9100)

        self.write_distribution([self.nippou(port=9002)])
        tool_registry.initialize()
        self.assertEqual(tool_registry.get(NIPPOU).port, 9002)

    def test_書かれていない項目には触らない(self) -> None:
        tool_registry.initialize()
        self.set_local(NIPPOU, start_args="--local", start_command=str(self.nippou_bat))
        self.write_distribution([self.nippou(port=9001)])
        tool_registry.initialize()

        tool = tool_registry.get(NIPPOU)
        self.assertEqual(tool.port, 9001)
        self.assertEqual(tool.start_args, "--local")
        self.assertEqual(tool.start_command, str(self.nippou_bat))

    def test_無い起動ファイルで端末の値を潰さない(self) -> None:
        """配った先で置き場所が違っても、その端末で直した値を残す。"""
        tool_registry.initialize()
        self.set_local(NIPPOU, start_command=str(self.nippou_bat))
        self.write_distribution([self.nippou(start_command="../無い/start.bat",
                                             port=9001)])
        tool_registry.initialize()

        tool = tool_registry.get(NIPPOU)
        self.assertEqual(tool.start_command, str(self.nippou_bat))
        self.assertEqual(tool.port, 9001)       # ほかの項目は入る

    def test_あとからツールを置けば空欄は埋まる(self) -> None:
        self.write_distribution([{
            "app_id": "nlm.kensa-tool", "display_name": "検査", "port": 8800,
            "start_command": "../検査/start.bat"}])
        tool_registry.initialize()
        self.assertFalse(tool_registry.get("nlm.kensa-tool").is_configured)

        self._make_bat("検査")                   # あとから置いた
        tool_registry.initialize()
        self.assertTrue(tool_registry.get("nlm.kensa-tool").is_configured)

    def test_消したツールは配り直すまで戻らない(self) -> None:
        self.write_distribution([self.nippou(port=9001)])
        tool_registry.initialize()
        tool_registry.delete_tool(NIPPOU)
        tool_registry.initialize()
        self.assertIsNone(tool_registry.get(NIPPOU))

        self.write_distribution([self.nippou(port=9002)])
        tool_registry.initialize()
        self.assertEqual(tool_registry.get(NIPPOU).port, 9002)

    def test_配布設定に無いツールには触らない(self) -> None:
        tool_registry.initialize()
        self.set_local(CALENDAR, port=9300, enabled=False)
        self.write_distribution([self.nippou(port=9001)])
        tool_registry.initialize()
        tool = tool_registry.get(CALENDAR)
        self.assertEqual((tool.port, tool.enabled), (9300, False))

    def test_形の合わない値は飛ばす(self) -> None:
        """1項目の誤りで、そのツールの設定がまるごと壊れないように。"""
        tool_registry.initialize()
        before = tool_registry.get(NIPPOU)
        self.write_distribution([
            self.nippou(port="abc", enabled="yes", stop_method="kill",
                        order_no=True, display_name="", health_path=3,
                        repository="vba-daily-report-python-migration2"),
            {"app_id": "空白 のID", "display_name": "x"},
            {"display_name": "IDなし"},
        ])
        tool_registry.initialize()

        tool = tool_registry.get(NIPPOU)
        self.assertEqual(tool.port, before.port)
        self.assertEqual(tool.enabled, before.enabled)
        self.assertEqual(tool.stop_method, before.stop_method)
        self.assertEqual(tool.order_no, before.order_no)
        self.assertEqual(tool.display_name, before.display_name)
        self.assertEqual(tool.health_path, before.health_path)
        self.assertEqual(tool.repository, "vba-daily-report-python-migration2")
        self.assertIsNone(tool_registry.get("空白 のID"))

    def test_パスワードを変えただけでは読み込み直さない(self) -> None:
        self.write_distribution([self.nippou(port=9001)])
        tool_registry.initialize()
        self.set_local(NIPPOU, port=9100)
        distribution.set_password("abcd")
        tool_registry.initialize()
        self.assertEqual(tool_registry.get(NIPPOU).port, 9100)

    def test_パスワードだけの配布設定では何も変えない(self) -> None:
        tool_registry.initialize()
        self.set_local(NIPPOU, port=9100)
        distribution.set_password("abcd")
        tool_registry.initialize()
        self.assertEqual(tool_registry.get(NIPPOU).port, 9100)

    def test_診断で配布設定の起動ファイルが見つからないことを出す(self) -> None:
        self.write_distribution([self.nippou(start_command="../無い/start.bat")])
        text = tool_registry.describe()
        self.assertIn("../無い/start.bat", text)
        self.assertIn("見つかりません", text)


# ------------------------------------------------------------------
# 通しで: 1台で整えて書き出し → フォルダーごと別の場所へ → 別の端末
# ------------------------------------------------------------------
class RoundTripTests(_Base):

    def setUp(self) -> None:
        super().setUp()
        # 配布設定は本来の場所 (ランチャーのフォルダー) に置く。
        # フォルダーごと運ばれることを確かめたい
        patcher = mock.patch.object(distribution, "path", REAL_DISTRIBUTION_PATH)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_配った先で同じ設定とパスワードになる(self) -> None:
        # --- 管理者の端末 ---
        self.assertTrue(admin_lock.unlock(*_pair(_Script("line-01", "line-01"))))
        tool_registry.initialize()
        tool_registry.set_start_command(NIPPOU, str(self.nippou_bat))
        tool_registry.set_start_command(CALENDAR, str(self.calendar_bat))
        self.set_local(NIPPOU, port=9001, display_name="日報(2ライン)")
        self.set_local(CALENDAR, enabled=False)
        tool_registry.export_distribution()

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


def _pair(script: _Script):
    return script.ask, script.tell
