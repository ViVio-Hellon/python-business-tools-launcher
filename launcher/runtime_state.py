"""ランチャーが起動した業務ツールの記録 (要件定義書 §10)

    アプリID / プロセスID / 使用ポート / 起動時刻 / 状態 /
    起動対象フォルダ / 起動用BAT

ファイルへ書くのは、**GUIとは別のプロセスから止められるようにする**
ため。`stop.bat` は `process_manager.py` をコンソールから呼ぶので、
GUIのメモリ上にしか記録が無いと、そこからは何も止められない。

置き場所はユーザー別ローカル領域の `runtime` (基盤仕様書 4.6)。
アプリ停止後は消してよい情報しか入れない。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import app_config
from .logging_utils import get_logger

log = get_logger("runtime_state")

# 動いているツールの記録。**同時に複数動く**ので、アプリIDごとに持つ
STATE_FILE = "running.json"
# 以前の版 (同時に1つだけ) の記録。残っていれば読み、次に書くときに消す
LEGACY_FILE = "current.json"
FORMAT = 1


@dataclass
class RunningTool:
    """ランチャーが起動した1つの業務ツール。"""

    app_id: str
    display_name: str = ""
    # 業務ツール本体(Webサーバ)のPID。**`/api/health` が返した値**で、
    # ランチャーが起こしたプロセスのPIDとは別物。止めるときはこちらを見る
    pid: int = 0
    # ランチャーが `start.bat` を起こしたときのPID (cmd.exe)。
    # ツールが応答しないときの後片付けに使う
    launch_pid: int = 0
    port: int = 0
    url: str = ""
    health_url: str = ""
    # 相手ツールの置き場所。**PIDで止める前の照合に使う** (要件定義書 §10)
    app_root: str = ""
    start_command: str = ""
    work_dir: str = ""
    stop_command: str = ""
    stop_method: str = "auto"
    # --- ランチャーが開いたブラウザー画面 (要件定義書 §8.3) ---
    # 専用プロファイルのアプリウィンドウとして開けたときだけ入る。
    # 既定ブラウザーへ渡しただけのときは 0 / 空 のまま
    browser_pid: int = 0
    # 停止前の照合の印。**この道がコマンドラインに入っている
    # プロセスだけを閉じてよい** (要件定義書 §8.3「利用者が別途開いて
    # いるブラウザーまで終了させてはいけない」)
    browser_profile: str = ""
    started_at: float = field(default_factory=time.time)
    # この記録を書いたランチャーのPID。判断には使わず、調査用に残す ──
    # 「いつのランチャーが起動したものか」が分かると、記録だけ残って
    # いる場面の切り分けが早い
    owner_pid: int = field(default_factory=os.getpid)
    # 画面の出し方。"browser" (ランチャーがブラウザーで開く) か
    # "app" (Tauri などの exe が自分の窓を出す)
    ui_mode: str = "browser"
    # 起動確認 (/api/health) で**確かめられた**か。窓が出た・プロセスが
    # 動いていることで「起動した」とみなしたものは偽。確かめられるまでは、
    # 応答が無くても「落ちた」とは判断しない (プロセスの生死で見る)
    confirmed: bool = True

    @property
    def started_text(self) -> str:
        return time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(self.started_at))

    @property
    def browser_managed(self) -> bool:
        """ランチャーが閉じられる画面か。"""
        return bool(self.browser_pid and self.browser_profile)

    @property
    def is_app(self) -> bool:
        """自分の窓を出すアプリか。"""
        return self.ui_mode == "app"

    @property
    def window_pid(self) -> int:
        """アプリの窓を持つプロセス (exe そのもの)。

        Web サーバーを中に持つアプリでは、`pid` は `/api/health` が返した
        サーバー部分のPIDで、窓は**ランチャーが起こした exe** が持つ。
        """
        return self.launch_pid or self.pid

    @property
    def watches_window(self) -> bool:
        """生死を窓とプロセスで見るか (/api/health が無い)。"""
        return self.is_app and not self.health_url

    def summary(self) -> str:
        window = f" / 画面 PID {self.browser_pid}" if self.browser_pid else ""
        if self.is_app:
            window = " / アプリの窓"
        port = f"ポート {self.port}" if self.port else (
            "Web サーバーなし" if self.is_app else "ポート ?")
        return (f"{self.display_name or self.app_id} "
                f"(PID {self.pid or '?'} / {port}{window} / "
                f"{self.started_text})")


def state_path() -> Path:
    return app_config.local_dir("runtime") / STATE_FILE


def _legacy_path() -> Path:
    return app_config.local_dir("runtime") / LEGACY_FILE


# 同じランチャーの中で、起動と停止が別々のスレッドから同時に書きに来る。
# 読んで・直して・書くあいだに割り込まれると、片方の書き込みが消える
_LOCK = threading.RLock()


def read_all() -> dict[str, RunningTool]:
    """動いているツールの記録をすべて読む。アプリIDごと。

    **読めないことを理由に止まらない。** 記録はあくまで手掛かりで、
    正しさの最終判断は `/api/health` の応答で行う。

    以前の版 (同時に1つだけ) の `current.json` が残っていれば、それも読む。
    """
    with _LOCK:
        found: dict[str, RunningTool] = {}
        raw = _read_json(state_path())
        if isinstance(raw, dict):
            for item in (raw.get("tools") or {}).values():
                running = _from_dict(item)
                if running is not None:
                    found[running.app_id] = running
        legacy = _from_dict(_read_json(_legacy_path()))
        if legacy is not None and legacy.app_id not in found:
            found[legacy.app_id] = legacy
        return found


def put(running: RunningTool) -> None:
    """1つのツールの記録を書く (あれば置き換える)。"""
    with _LOCK:
        records = read_all()
        records[running.app_id] = running
        _write_all(records)
    log.info("実行中の記録: %s", running.summary())


def remove(app_id: str) -> None:
    """1つのツールの記録を消す。"""
    with _LOCK:
        records = read_all()
        if records.pop(app_id, None) is not None:
            _write_all(records)
            log.info("実行中の記録を消しました: %s", app_id)


def replace_all(records: dict[str, RunningTool]) -> None:
    """記録を丸ごと入れ替える (起動時の引き継ぎで、応答しないものを落とす)。"""
    with _LOCK:
        _write_all(dict(records))


def clear() -> None:
    """記録をすべて消す。"""
    with _LOCK:
        _write_all({})


# --- 以前の呼び方 (同時に1つだけの頃)。試験と診断のために残す ---
def read() -> Optional[RunningTool]:
    """いちばん新しく起動したツールの記録。無ければ `None`。"""
    records = read_all()
    if not records:
        return None
    return max(records.values(), key=lambda r: r.started_at)


def write(running: Optional[RunningTool]) -> None:
    """`None` ならすべて消す。そうでなければ1つ書く。"""
    if running is None:
        clear()
    else:
        put(running)


# ------------------------------------------------------------------
def _write_all(records: dict[str, RunningTool]) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if records:
            data = {"format": FORMAT,
                    "tools": {k: asdict(v) for k, v in records.items()}}
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            os.replace(temporary, path)
        else:
            _unlink(path)
        # 以前の版の記録は、読み込んだ時点でこちらへ移っている
        _unlink(_legacy_path())
    except OSError as exc:
        log.warning("実行中の記録を書けませんでした: %s", exc)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("実行中の記録を読めませんでした (%s): %s", path.name, exc)
        return None


def _from_dict(raw) -> Optional[RunningTool]:
    if not isinstance(raw, dict):
        return None
    known = {k: raw[k] for k in RunningTool.__dataclass_fields__ if k in raw}
    try:
        return RunningTool(**known)
    except TypeError as exc:
        log.warning("実行中の記録の形が違います: %s", exc)
        return None
