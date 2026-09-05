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
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import app_config
from .logging_utils import get_logger

log = get_logger("runtime_state")

STATE_FILE = "current.json"


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
    started_at: float = field(default_factory=time.time)
    # この記録を書いたランチャーのPID。判断には使わず、調査用に残す ──
    # 「いつのランチャーが起動したものか」が分かると、記録だけ残って
    # いる場面の切り分けが早い
    owner_pid: int = field(default_factory=os.getpid)

    @property
    def started_text(self) -> str:
        return time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(self.started_at))

    def summary(self) -> str:
        return (f"{self.display_name or self.app_id} "
                f"(PID {self.pid or '?'} / ポート {self.port or '?'} / "
                f"{self.started_text})")


def state_path() -> Path:
    return app_config.local_dir("runtime") / STATE_FILE


def write(running: Optional[RunningTool]) -> None:
    """いま動いているツールを記録する。`None` で消す。"""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if running is None:
        try:
            path.unlink()
            log.info("実行中の記録を消しました")
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("実行中の記録を消せませんでした: %s", exc)
        return

    try:
        path.write_text(json.dumps(asdict(running), ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError as exc:
        log.warning("実行中の記録を書けませんでした: %s", exc)
        return
    log.info("実行中の記録: %s", running.summary())


def read() -> Optional[RunningTool]:
    """記録を読む。壊れていれば `None`。

    **読めないことを理由に止まらない。** 記録はあくまで手掛かりで、
    正しさの最終判断は `/api/health` の応答で行う。
    """
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("実行中の記録を読めませんでした: %s", exc)
        return None
    if not isinstance(raw, dict):
        return None
    known = {k: raw[k] for k in RunningTool.__dataclass_fields__ if k in raw}
    try:
        return RunningTool(**known)
    except TypeError as exc:
        log.warning("実行中の記録の形が違います: %s", exc)
        return None


def clear() -> None:
    write(None)
