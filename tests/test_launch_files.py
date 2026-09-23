"""起動ファイルの文字コードと改行 (要件定義書 §17)

`.bat` は cmd.exe が**コンソールのコードページ**で、`.vbs` は WSH が
**システムANSI**で読む。日本語Windowsではどちらも CP932 なので、
UTF-8 で保存し直すと現場で文字化けする。

`.bat` にはもう1つ順序の決まりがある。`chcp 932` より前に非ASCIIの
バイトがあると、その部分は**違うコードページで解釈される**。CP932 の
2バイト目には 0x5C (`\\`) や 0x40 (`@`) が現れるため、コメントの
文字が区切り記号に化けて構文が壊れる。
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LAUNCH_FILES = ("Start.vbs", "start_debug.bat", "stop.bat")


class EncodingTests(unittest.TestCase):

    def test_起動ファイルが揃っている(self) -> None:
        """通常起動・診断起動・停止の3つ (要件定義書 §17 / §18)。"""
        for name in LAUNCH_FILES:
            with self.subTest(name):
                self.assertTrue((ROOT / name).is_file(), f"{name} がありません")

    def test_CP932で読める(self) -> None:
        for name in LAUNCH_FILES:
            with self.subTest(name):
                data = (ROOT / name).read_bytes()
                try:
                    data.decode("cp932")
                except UnicodeDecodeError as exc:
                    self.fail(f"{name} は CP932 で読めません: {exc}")

    def test_UTF8のBOMが付いていない(self) -> None:
        """BOM が付くと cmd.exe が1行目を読み違える。"""
        for name in LAUNCH_FILES:
            with self.subTest(name):
                data = (ROOT / name).read_bytes()
                self.assertFalse(data.startswith(b"\xef\xbb\xbf"),
                                 f"{name} に BOM が付いています")

    def test_改行がCRLF(self) -> None:
        for name in LAUNCH_FILES:
            with self.subTest(name):
                data = (ROOT / name).read_bytes()
                lone_lf = data.replace(b"\r\n", b"").count(b"\n")
                self.assertEqual(lone_lf, 0,
                                 f"{name} に CRLF でない改行があります")


class BatchOrderTests(unittest.TestCase):
    """`.bat` は chcp より前を ASCII に保つ。"""

    def test_chcpより前が全てASCII(self) -> None:
        for name in ("start_debug.bat", "stop.bat"):
            with self.subTest(name):
                data = (ROOT / name).read_bytes()
                self.assertIn(b"chcp 932", data, f"{name} に chcp がありません")
                head = data.split(b"chcp 932")[0]
                bad = [b for b in head if b > 0x7F]
                self.assertEqual(
                    bad, [],
                    f"{name} の chcp より前に非ASCIIのバイトがあります")


class ContentTests(unittest.TestCase):
    """中身が意図した入口を指しているか。"""

    def test_通常起動はコンソールを出さない(self) -> None:
        """要件定義書 §17「通常利用では、不要なコンソール画面を表示しない」。"""
        text = (ROOT / "Start.vbs").read_text(encoding="cp932")
        self.assertIn("pythonw", text, "Start.vbs が pythonw を使っていません")
        self.assertIn("launcher.py", text)

    def test_診断起動はコンソールを出す(self) -> None:
        text = (ROOT / "start_debug.bat").read_text(encoding="cp932")
        self.assertIn("launcher.py", text)
        self.assertNotIn("pythonw", text,
                         "診断起動が pythonw を使うと、出力が誰にも見えません")

    def test_停止はプロセス管理を呼ぶ(self) -> None:
        text = (ROOT / "stop.bat").read_text(encoding="cp932")
        self.assertIn("process_manager.py", text)
        # プロセス名での一括終了を禁じている (要件定義書 §21)
        self.assertNotIn("taskkill /IM", text)
        self.assertNotIn("python.exe", text)


if __name__ == "__main__":
    unittest.main()


class ReadmeJsonTests(unittest.TestCase):
    """README の JSON 例がそのまま使えること。

    **JSON はコメント (`//`) を許さない。** 例にコメントを書くと、
    そのまま `config/launcher.json` へ写したときに設定が壊れ、新しい
    端末ではツールのボタンが1つも出なくなる。実際にそうなっていた。
    """

    def test_READMEのJSONはすべて読める(self) -> None:
        import json
        import re

        text = (ROOT / "README.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```json\n(.*?)```", text, flags=re.S)
        self.assertTrue(blocks, "README に JSON の例がありません")

        for index, block in enumerate(blocks, 1):
            with self.subTest(block=index):
                self.assertNotIn("//", block,
                                 f"{index}つめの例にコメントがあります")
                # 断片 ("ui": {...}) も、外側を補えば読めること
                for candidate in (block, "{" + block + "}"):
                    try:
                        json.loads(candidate)
                        break
                    except json.JSONDecodeError:
                        continue
                else:
                    self.fail(f"{index}つめの例が JSON として読めません:\n{block}")

    def test_同梱の設定ファイルが読める(self) -> None:
        import json

        json.loads((ROOT / "config" / "launcher.json").read_text(encoding="utf-8"))
