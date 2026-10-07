"""試験が本番のローカル領域を汚さないようにする土台。

`%LOCALAPPDATA%\\BusinessToolsLauncher` には、設定DBと実行中の記録が
入っている。試験がそこを触ると、**開発機で使っているランチャーの設定が
消える**。環境変数でローカル領域を一時フォルダへ逃がす。
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from launcher import app_config, distribution, trace  # noqa: E402

# 本来の置き場所 (ランチャーのフォルダーの中)。フォルダーごと運ぶ
# 試験ではこちらに戻す
REAL_DISTRIBUTION_FOLDER = distribution.folder


class LocalAreaTestCase(unittest.TestCase):
    """ローカル領域を一時フォルダへ逃がす。"""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.local_root = Path(self._tmp.name) / "local"
        self.work_root = Path(self._tmp.name) / "tools"
        self.work_root.mkdir(parents=True, exist_ok=True)

        self._orig = os.environ.get(app_config.LOCAL_DIR_ENV)
        os.environ[app_config.LOCAL_DIR_ENV] = str(self.local_root)
        app_config.ensure_local_dirs()

        # 製品の既定値のツール一覧を、試験用の4ツールにする。出荷する
        # 設定は空だが、「既定値に書けば各端末へ入る」仕組みは残して
        # あるので、それを確かめる試験のために置く
        from _sample_tools import SAMPLE_TOOLS

        config = app_config.load()
        original_tools = config.get("tools")
        config["tools"] = copy.deepcopy(SAMPLE_TOOLS)
        self.addCleanup(config.__setitem__, "tools", original_tools)

        # 配布先フォルダも一時フォルダーへ。開発機で作った配布先フォルダ
        # (`distribution/`) が試験に混ざらないように
        self.distribution_folder = (Path(self._tmp.name) / "app"
                                    / distribution.FOLDER_NAME)
        patcher = mock.patch.object(distribution, "folder",
                                    lambda: self.distribution_folder)
        patcher.start()
        self.addCleanup(patcher.stop)

        # 記録の置き場所は一度決めると覚えておく作り。前の試験が決めた
        # 場所 (消えた一時フォルダー) を引きずらないよう、試験ごとに忘れる
        trace._destination = None
        self.addCleanup(setattr, trace, "_destination", None)
        trace._seen_unexpected.clear()
        # 「確かめられなかった起動ファイル」の覚えも試験ごとに忘れる
        from launcher import fileprobe
        fileprobe.forget()
        self.addCleanup(fileprobe.forget)

    def tearDown(self) -> None:
        if self._orig is None:
            os.environ.pop(app_config.LOCAL_DIR_ENV, None)
        else:
            os.environ[app_config.LOCAL_DIR_ENV] = self._orig
        self._tmp.cleanup()
        super().tearDown()
