"""どの版が入っていて、どの版が動いているか

現場でいちばん困るのは「入れ替えたはずなのに直っていない」状態。原因は
たいてい次のどちらかで、**見分けがつかないと端末まで見に行くことになる**。

    ・入れ替えたつもりで別のフォルダーを起動していた
    ・入れ替えたが、古いプロセスが動いたままだった

そこで2つの版を並べて出す。

    入っている版 … `start.bat` の隣の `config/app.json` の version
    動いている版 … `/api/health` が返す version

食い違っていれば、**古いプロセスが残っている**と分かる。

このモジュールは tkinter に触らない。画面 (バージョン情報ダイアログ) と
コンソール (`start_debug.bat --check`) の両方が、同じ中身を使う。
"""
from __future__ import annotations

import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import app_config, health, tool_registry
from .logging_utils import get_logger

log = get_logger("version_info")


@dataclass
class ToolVersion:
    """1つの業務ツールの版と居場所。"""

    app_id: str
    display_name: str
    # `config/app.json` から読んだ、いま置かれている版
    installed: str = ""
    # `/api/health` が返した、いま動いている版
    running: str = ""
    port: int = 0
    app_root: str = ""
    configured: bool = False
    alive: bool = False

    @property
    def mismatched(self) -> bool:
        """入っている版と動いている版が食い違っているか。

        **古いプロセスが残っている合図。** 入れ替えたのに直らない、の
        典型的な原因なので、見つけたら目立たせる。
        """
        return bool(self.installed and self.running
                    and self.installed != self.running)

    @property
    def state(self) -> str:
        if not self.configured:
            return "未設定"
        if not self.alive:
            return "停止中"
        return "版ちがい" if self.mismatched else "動作中"

    @property
    def version_text(self) -> str:
        """一覧に出す版の文字。"""
        if self.mismatched:
            return f"{self.installed} → 動作中は {self.running}"
        return self.installed or self.running or "—"


@dataclass
class LauncherVersion:
    """ランチャー自身。"""

    version: str
    app_root: str
    python: str
    local_root: str
    problem: str = ""


def launcher() -> LauncherVersion:
    return LauncherVersion(
        version=app_config.version_label(),
        app_root=str(app_config.APP_ROOT),
        python=f"{sys.version.split()[0]} ({sys.executable})",
        local_root=str(app_config.local_root()),
        problem=app_config.version_problem())


def tools(*, probe_running: bool = True) -> list[ToolVersion]:
    """登録されている全ツールの版を集める。

    `probe_running=False` にすると `/api/health` を叩かない。動いている
    ものを知る必要がなく、待ち時間を惜しむ場面のため。
    """
    found: list[ToolVersion] = []
    for tool in tool_registry.all_tools(include_disabled=True):
        item = ToolVersion(app_id=tool.app_id,
                           display_name=tool.display_name,
                           port=tool.port,
                           configured=tool.is_configured)
        if tool.is_configured:
            probed = tool_registry.probe_tool_folder(tool.start_command)
            item.installed = str(probed.get("version", ""))
            item.app_root = tool.resolved_work_dir

        if probe_running:
            payload = health.probe(tool.health_url)
            if health.is_tool(payload, tool.app_id):
                item.alive = True
                item.running = str((payload or {}).get("version", ""))
                # **動いているほうの場所を優先する。** 設定と違う
                # フォルダーが動いていることがあり、それ自体が答えになる
                root = str((payload or {}).get("app_root", ""))
                if root:
                    item.app_root = root
        found.append(item)
    return found


def describe(*, probe_running: bool = True) -> str:
    """コンソールに出す1枚 (`start_debug.bat --check`)。"""
    info = launcher()
    lines = [
        "=== バージョン情報 ===",
        f"ランチャー    : {info.version}",
        f"アプリ本体    : {info.app_root}",
        f"Python        : {info.python}",
    ]
    if info.problem:
        lines.append(f"版の問題      : {info.problem}")

    lines.append("")
    lines.append("業務ツール:")
    items = tools(probe_running=probe_running)
    if not items:
        lines.append("  (登録されているツールがありません)")
        return "\n".join(lines)

    width = max(display_width(t.display_name) for t in items)
    indent = " " * (width + 2)
    for item in items:
        lines.append(f"  {pad(item.display_name, width)}  "
                     f"[{pad(item.state, 6)}]  {item.version_text}")
        if item.app_root:
            lines.append(f"  {indent}         {item.app_root}")
        if item.mismatched:
            lines.append(f"  {indent}         "
                         "※古いプロセスが動いたままです。"
                         "stop.bat で止めてから起動し直してください")
    return "\n".join(lines)


# ------------------------------------------------------------------
# 桁揃え
# ------------------------------------------------------------------
def display_width(text: str) -> int:
    """コンソールで見たときの桁数。

    `len()` は全角も1と数えるので、日本語の表示名が並ぶと桁が揃わない。
    「日報」と「カレンダー」を同じ列に出すために、全角を2と数える。
    """
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in text)


def pad(text: str, width: int) -> str:
    """右に空白を足して桁を揃える。"""
    return text + " " * max(0, width - display_width(text))
