#!/usr/bin/env python3
"""ランチャー自身の多重起動防止 (基盤仕様書 2.4)

ランチャーを二重に起動すると、画面が2枚重なるだけでなく、**どちらが
どのツールを起動したのかが分からなくなる**。片方が止めようとした
ツールを、もう片方が「動いている」と表示し続ける状態になる。

判定は2段構え:

    1. ロックファイルのPIDのプロセスが存在するか
    2. そのプロセスが**このランチャー**か (コマンドラインを照合)

1だけでは足りない。PIDは使い回されるので、無関係なプロセスが同じ番号を
持っていることがある。そうなると、ランチャーが二度と起動しなくなる。

業務ツール側の多重起動防止は、それぞれのツールが自分で持っている
(4つとも同じ起動基盤を使っている)。ここが見るのはランチャー自身だけ。
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from launcher import app_config  # noqa: E402
from launcher.logging_utils import get_logger  # noqa: E402

log = get_logger("launch_guard")

LOCK_NAME = "launcher.lock"


@dataclass
class LockInfo:
    """`runtime/launcher.lock` の中身。"""

    app_id: str
    pid: int
    started_at: float
    python: str = ""
    app_root: str = ""

    @property
    def started_text(self) -> str:
        return time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(self.started_at))


@dataclass
class GuardResult:
    """起動してよいか、と理由。"""

    should_start: bool
    reason: str
    existing: Optional[LockInfo] = None


def lock_path() -> Path:
    return app_config.local_dir("runtime") / LOCK_NAME


def build_lock_info() -> LockInfo:
    return LockInfo(
        app_id=app_config.app_id(),
        pid=os.getpid(),
        started_at=time.time(),
        python=sys.executable,
        app_root=str(app_config.APP_ROOT),
    )


def write_lock(info: Optional[LockInfo] = None) -> Path:
    info = info or build_lock_info()
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(info), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    log.info("ロックを書きました: %s (pid=%s)", path, info.pid)
    return path


def read_lock() -> Optional[LockInfo]:
    """壊れていれば `None`。読めないことを理由に起動を止めない。"""
    try:
        raw = json.loads(lock_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("ロックを読めませんでした: %s", exc)
        return None
    known = {k: raw[k] for k in LockInfo.__dataclass_fields__ if k in raw}
    try:
        return LockInfo(**known)
    except TypeError:
        return None


def remove_lock() -> None:
    try:
        lock_path().unlink()
        log.info("ロックを消しました")
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("ロックを消せませんでした: %s", exc)


def check_existing() -> GuardResult:
    """すでにランチャーが動いていないか (基盤仕様書 2.4)。"""
    info = read_lock()
    if info is None:
        return GuardResult(True, "ロックがありません")

    if info.pid == os.getpid():
        return GuardResult(True, "自分自身のロックです")

    # `process_manager` の判定をそのまま使う。**同じ照合を2か所に
    # 書かない** ── 片方だけ直した状態を作らないため
    import process_manager

    if not process_manager._is_alive(info.pid):
        remove_lock()
        return GuardResult(True, f"残っていたロックを片付けました (pid={info.pid})")

    command = process_manager.process_command_line(info.pid)
    if command and _looks_like_launcher(command, info):
        return GuardResult(False,
                           f"すでに起動しています (pid={info.pid} / "
                           f"{info.started_text})", info)

    if not command:
        # 中身を確かめられない。**起動を止めるほうに倒す** ── 本当に
        # 動いているランチャーを2つにするより、起動しないほうがよい。
        # 利用者にはPIDと片付け方を出す
        return GuardResult(False,
                           f"pid={info.pid} が何かを確かめられませんでした。"
                           f"動いていなければ {lock_path()} を削除してください",
                           info)

    remove_lock()
    return GuardResult(True, f"pid={info.pid} は別のプロセスでした")


def _looks_like_launcher(command: str, info: LockInfo) -> bool:
    """そのコマンドラインがこのランチャーのものか。"""
    import process_manager

    haystack = process_manager._normalize_path(command)
    needle = process_manager._normalize_path(info.app_root or str(APP_ROOT))
    return bool(needle) and needle in haystack
