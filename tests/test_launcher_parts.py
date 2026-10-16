"""ランチャー自身の部品が足りないとき (1.7.7)

all-tools で、本体を組み立てられなかったツールが待機画面のまま残った。ランチャー
自身は組み立てに失敗すると終わる (残らない) が、**障害記録を書くだけで、何も
見せずに消えていた** (`Start.vbs` は pythonw なので、トレースバックも見えない)。
何が無くて、どうすればよいか (フォルダーを丸ごと入れ直す) を出す。
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _load_main():
    spec = importlib.util.spec_from_file_location("launcher_main", ROOT / "launcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PartsListTests(unittest.TestCase):

    def test_部品の一覧が実際のファイルとそろっている(self) -> None:
        """部品を足したのに一覧に足し忘れると、欠けても名指しできない。"""
        main = _load_main()
        actual = {str(p.relative_to(ROOT)).replace("\\", "/")
                  for p in (ROOT / "launcher").rglob("*.py")
                  if "__pycache__" not in p.parts}
        listed = set(main.ESSENTIAL_FILES)
        self.assertEqual(actual - listed, set(), "一覧に無い部品があります")
        self.assertEqual(main.missing_parts(), [])


class ReportTests(LocalAreaTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.main = _load_main()
        self.reported = []
        patches = [
            mock.patch.object(self.main, "run_environment_checks"),
            mock.patch.object(self.main, "report_failure",
                              side_effect=lambda error, incident="":
                              self.reported.append((error, incident))),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_main(self, exc: BaseException) -> int:
        with mock.patch.object(self.main, "start", side_effect=exc):
            return self.main.main([])

    def test_部品が無ければ名指しして入れ直すよう言う(self) -> None:
        exc = ImportError("cannot import name 'bar' from 'launcher.ui'")
        exc.launcher_incident = "/logs/incidents/x.txt"
        with mock.patch.object(self.main, "missing_parts",
                               return_value=["launcher/ui/bar.py"]):
            self.assertEqual(self.run_main(exc), 1)
        error, incident = self.reported[0]
        self.assertEqual(str(error), "ランチャーの部品が足りないため、起動できませんでした")
        self.assertIn("見つからない部品: launcher/ui/bar.py", error.hint)
        self.assertIn("丸ごと入れ直してください", error.hint)
        self.assertEqual(incident, "/logs/incidents/x.txt", "障害記録を重ねて書かない")

    def test_部品はあるが読めないときも言う(self) -> None:
        self.run_main(ModuleNotFoundError("No module named 'launcher.ui.bar'"))
        error, _ = self.reported[0]
        self.assertIn("読み込めなかった部品: No module named 'launcher.ui.bar'", error.hint)

    def test_部品と関係の無い失敗は開発担当へ(self) -> None:
        self.run_main(ValueError("設定の値が変"))
        error, _ = self.reported[0]
        self.assertIn("ランチャーを組み立てられませんでした: ValueError: 設定の値が変",
                      str(error))
        self.assertIn("開発担当", error.hint)


if __name__ == "__main__":
    unittest.main()
