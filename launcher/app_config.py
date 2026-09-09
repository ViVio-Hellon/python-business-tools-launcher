"""ランチャー自身の設定とユーザー別ローカル領域 (基盤仕様書 2.7 / 4.6)

設定の出どころは `config/launcher.json` ただ1つ。アプリID・表示名・
使用ポートを複数のファイルへ書き散らさない (基盤仕様書 ステップ2)。

実行時に増えるもの (ログ・設定DB・プロセス情報) は、アプリ本体では
なく `%LOCALAPPDATA%\\BusinessToolsLauncher` へ置く。ランチャー本体を
共有フォルダーに置いて複数人が使う運用があり、そこへログを書くと
同期競合とアクセス権の問題を起こすため。

    %LOCALAPPDATA%\\BusinessToolsLauncher\\
      runtime\\  … 起動中のツールの記録・ランチャーのロック
      logs\\     … 起動/停止/エラーの記録 (要件定義書 §16)
      pycache\\  … Pythonのバイトコード
      cache\\    … 再取得できる高速化用データ
      work\\     … 一時ファイル
      backup\\   … 設定DBの控え
      data\\     … 設定DB本体 (消すと設定が消える)
      browser\\  … 業務ツールの画面を開くための専用プロファイル。
                  利用者のふだんのブラウザーとは完全に別 (要件定義書 §8.3)
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# このファイルの1つ上がアプリ本体の置き場所。
# `__file__` から辿るのは、共有フォルダーやショートカット経由で
# 起動されても正しい場所を指すため (カレントディレクトリは当てにしない)
APP_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_ROOT / "config" / "launcher.json"

# ローカル領域を丸ごと差し替える環境変数。試験で本番の
# `%LOCALAPPDATA%` を汚さないための逃げ道
LOCAL_DIR_ENV = "BUSINESS_TOOLS_LAUNCHER_LOCAL_DIR"

# 設定ファイルが読めないときに使う値。**起動しないより、既定で
# 起動して設定画面まで到達できるほうがよい** (利用者は設定画面で
# start.bat のパスを入れるところまで進める)
_FALLBACK: dict[str, Any] = {
    "app_id": "nlm.business-tools-launcher",
    "display_name": "業務ツール統合ランチャー",
    "version": "0.0.0",
    "local_dir_name": "BusinessToolsLauncher",
    "ui": {
        "bar_height": 56,
        "bottom_margin": 48,
        "edge_margin": 16,
        "position_idle": "center",
        "position_active": "bottom_left",
        "move_animation_ms": 180,
        "health_poll_seconds": 5,
        "start_timeout_seconds": 90,
        "stop_timeout_seconds": 30,
    },
    "tools": [],
}

_cache: dict[str, Any] | None = None
_load_error: str = ""


def load(*, reload: bool = False) -> dict[str, Any]:
    """`config/launcher.json` を読む。壊れていても例外にしない。

    設定ファイルが壊れているときに起動そのものが落ちると、利用者には
    「ランチャーが起動しない」としか見えない。既定値で起動して、
    画面と `load_error()` で理由を出すほうが直しに繋がる
    (基盤仕様書 ステップ5「設定ファイルが壊れている」場合の見え方)。
    """
    global _cache, _load_error
    if _cache is not None and not reload:
        return _cache

    _load_error = ""
    data = dict(_FALLBACK)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("最上位がオブジェクトではありません")
        data.update(raw)
        # `ui` は部分的な上書きを許す。全部書かないと既定が消えるのは
        # 設定ファイルとして扱いにくい
        merged_ui = dict(_FALLBACK["ui"])
        merged_ui.update(raw.get("ui") or {})
        data["ui"] = merged_ui
    except FileNotFoundError:
        _load_error = f"設定ファイルがありません: {CONFIG_PATH}"
    except (OSError, ValueError) as exc:
        _load_error = f"設定ファイルを読めません ({CONFIG_PATH}): {exc}"

    _cache = data
    return _cache


def load_error() -> str:
    """設定の読み込みで起きた問題。無ければ空文字。"""
    load()
    return _load_error


def app_id() -> str:
    """ランチャー自身のアプリID。**業務ツールのIDとは別物**。"""
    return str(load()["app_id"])


def display_name() -> str:
    return str(load()["display_name"])


def version() -> str:
    return str(load()["version"])


# 版のバッジに付ける頭。画面で「数字の羅列」に見えないようにする
VERSION_PREFIX = "v"
_VERSION_FORM = re.compile(r"^\d+\.\d+\.\d+$")


def version_label() -> str:
    """画面のバッジに出す形。例 `v1.0.0`。"""
    return f"{VERSION_PREFIX}{version()}"


def version_problem() -> str:
    """版の書き方がおかしければ理由。正しければ空文字。

    番号が読めない形だと「どれが新しいのか」を並べて比べられなくなる。
    起動は止めない (版が読めなくてもランチャーは使える) が、
    診断とバージョン情報には出す。
    """
    text = version()
    if _VERSION_FORM.match(text):
        return ""
    return (f"版の書き方が違います: {text!r}。"
            "config/launcher.json の version は「1.0.0」のように"
            "数字3つで書いてください。")


def ui_setting(name: str) -> Any:
    return load()["ui"][name]


def default_tools() -> list[dict[str, Any]]:
    """同梱の既定ツール定義。設定DBの初回投入だけに使う。

    ここを直接読むのは `tool_registry` の初回投入のみ。**動作中の
    参照先は常に設定DB**にする ── 端末ごとに `start.bat` の場所が
    違い、そちらが正しい値だから (要件定義書 §13.3)。
    """
    tools = load().get("tools") or []
    return [dict(t) for t in tools if isinstance(t, dict)]


# ------------------------------------------------------------------
# ユーザー別ローカル領域
# ------------------------------------------------------------------
def local_root() -> Path:
    """`%LOCALAPPDATA%\\<local_dir_name>` (非Windowsは XDG 相当)。"""
    override = os.environ.get(LOCAL_DIR_ENV)
    if override and override.strip():
        return Path(override.strip())

    name = str(load()["local_dir_name"])
    base = os.environ.get("LOCALAPPDATA")
    if base:                                    # Windows
        return Path(base) / name
    # Linux/macOS。開発機と試験用
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / name
    return Path.home() / ".local" / "share" / name


LOCAL_SUBDIRS = ("runtime", "logs", "pycache", "cache", "work", "backup",
                 "data", "browser")


def local_dir(name: str) -> Path:
    """ローカル領域の中のフォルダを1つ取る。"""
    if name not in LOCAL_SUBDIRS:
        raise ValueError(
            f"未知のローカル領域: {name!r} (使えるのは {', '.join(LOCAL_SUBDIRS)})")
    return local_root() / name


def ensure_local_dirs() -> Path:
    """ローカル領域を作る。既にあれば何もしない。"""
    root = local_root()
    for name in LOCAL_SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def settings_db_path() -> Path:
    """ツール設定の保存先。**消すと設定が消える**ので `data` に置く。"""
    return local_dir("data") / "launcher.db"


def describe() -> str:
    """診断用の1枚。`start_debug.bat` とログの先頭に出す (基盤仕様書 2.6)。"""
    import sys

    lines = [
        f"アプリID      : {app_id()}",
        f"表示名        : {display_name()}",
        f"バージョン    : {version_label()}",
        f"アプリ本体    : {APP_ROOT}",
        f"設定ファイル  : {CONFIG_PATH}",
        f"ローカル領域  : {local_root()}",
        f"設定DB        : {settings_db_path()}",
        f"Python        : {sys.version.split()[0]} ({sys.executable})",
        f"プロセスID    : {os.getpid()}",
    ]
    if _load_error:
        lines.append(f"設定の問題    : {_load_error}")
    problem = version_problem()
    if problem:
        lines.append(f"版の問題      : {problem}")
    return "\n".join(lines)
