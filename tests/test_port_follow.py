"""ツールがポートを変えたら、登録をそれに合わせる (1.8.5)

DBビューア 0.37.0 は 8743〜8746 (RNEConverter・看板と重なっていた) から 8790〜8793 へ
移った。登録は登録したときの番号のままなので、そのままだと古い番号 (ほかのツールが
使っているかもしれない) に問い合わせる。ツールの `config/app.json` の番号に合わせる。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase  # noqa: E402

from launcher import tool_master, tool_registry  # noqa: E402
from launcher.tool_master import MasterTool  # noqa: E402
from launcher.tool_registry import Tool  # noqa: E402


def make_db_viewer(base: Path, *, port: int = 8790, roles: dict | None = None) -> Path:
    root = base / "db-viewer"
    (root / "config").mkdir(parents=True)
    server = {"host": "127.0.0.1", "port_retry": 3,
              "roles": roles or {"main": {"port": port}}}
    (root / "config" / "app.json").write_text(json.dumps(
        {"app_id": "nlm.db-viewer", "display_name": "DBビューア", "version": "0.37.0",
         "server": server}, ensure_ascii=False), encoding="utf-8")
    (root / "Start.vbs").write_text("' start\n", encoding="utf-8")
    (root / "DBビューア.exe").write_bytes(b"MZ" + b"\0" * 64)
    return root


class FollowTests(LocalAreaTestCase):

    def setUp(self) -> None:
        super().setUp()
        for tool in tool_registry.all_tools(include_disabled=True):
            tool_registry.delete_tool(tool.app_id)
        self.root = make_db_viewer(self.work_root)

    def register(self, start: str, port: int, app_id: str = "nlm.db-viewer") -> None:
        tool_registry.save(Tool(app_id=app_id, display_name="DBビューア", port=port,
                                start_command=start))

    def test_古い番号の登録をツールの番号に合わせる(self) -> None:
        self.register(str(self.root / "Start.vbs"), 8743)
        self.assertEqual(tool_registry.declared_port(str(self.root / "Start.vbs")), 8790)
        moved = tool_registry.follow_declared_ports()
        self.assertEqual(moved, ["DBビューア: 8743 → 8790"])
        self.assertEqual(tool_registry.get("nlm.db-viewer").port, 8790)
        self.assertEqual(tool_registry.follow_declared_ports(), [], "2回目も変えました")

    def test_exe_とポート空は触らない(self) -> None:
        self.register(str(self.root / "DBビューア.exe"), 8743)
        self.register(str(self.root / "Start.vbs"), 0, app_id="nlm.other")
        self.assertEqual(tool_registry.follow_declared_ports(), [])
        self.assertEqual(tool_registry.get("nlm.db-viewer").port, 8743)
        self.assertEqual(tool_registry.get("nlm.other").port, 0)

    def test_役割でポートが違うツールは触らない(self) -> None:
        root = make_db_viewer(self.work_root / "roles",
                              roles={"field": {"port": 8713}, "material": {"port": 8723}})
        self.register(str(root / "Start.vbs"), 8723)
        self.assertEqual(tool_registry.follow_declared_ports(), [])

    def test_起動ファイルが無ければ触らない(self) -> None:
        self.register(r"C:\\無い場所\\db-viewer\\Start.vbs", 8743)
        self.assertEqual(tool_registry.follow_declared_ports(), [])

    def test_起動したとき合わせて詳細で知らせる(self) -> None:
        import boot

        self.register(str(self.root / "Start.vbs"), 8743)
        result = boot.BootResult(started=True)
        boot._load_settings(result)
        self.assertEqual(tool_registry.get("nlm.db-viewer").port, 8790)
        self.assertTrue(any("8743 → 8790" in text for text in result.notices), result.notices)

    def test_マスタに古い番号が残っていてもツールの番号を使う(self) -> None:
        with mock.patch.dict(os.environ, {"USERNAME": "u", "COMPUTERNAME": "PC"}):
            shared = self.work_root / "share"
            tool_registry.set_pc_setting(tool_registry.TOOL_MASTER_KEY, str(shared))
            path = tool_master.path_for(str(shared))
            tool_master.save_tool(path, MasterTool(0, name="DBビューア",
                                                   start_command=str(self.root / "Start.vbs"),
                                                   app_id="nlm.db-viewer", port=8743))
            tool_master.sync()
            self.assertEqual(tool_registry.get("nlm.db-viewer").port, 8790)
            self.assertFalse(tool_master.sync().changed, "起動のたびに書き直しています")
            # ［設定］の一覧にも新しい番号で出る (保存するとマスタも 8790 になる)
            master = tool_master.read(path)
            shown = tool_master.as_tool(master.tools[0], tool_registry.all_tools())
            self.assertEqual(shown.port, 8790)


if __name__ == "__main__":
    unittest.main()
