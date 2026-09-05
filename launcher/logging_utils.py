"""ログ出力 (要件定義書 §16 / 基盤仕様書 2.6)

「起動しない」「切り替わらない」を、利用者の説明だけに頼らず調べられる
ようにするための記録。最低限これを残す:

    ランチャー起動日時 / アプリID / 起動開始 / 起動完了 / PID /
    ポート / 起動失敗 / 停止開始 / 停止完了 / 終了理由 / エラー内容

出力先はユーザー別ローカル領域の `logs` (基盤仕様書 2.7)。共有フォルダー
に置いたアプリ本体側へは書かない。
"""
from __future__ import annotations

import logging
import sys
from datetime import date

from . import app_config

_configured = False

# ログ名の親。`get_logger()` はこの下にぶら下げる
ROOT_NAME = "launcher"

# 何日分残すか。起動のたびに1行ずつ増える程度なので大きくは要らないが、
# 「先週から切り替わらない」の類は数日さかのぼれないと追えない
KEEP_DAYS = 30


def configure_logging(*, console: bool | None = None) -> None:
    """起動時に一度だけ呼ぶ。二重呼び出しは無害 (冪等)。"""
    global _configured
    if _configured:
        return

    app_config.ensure_local_dirs()
    log_file = app_config.local_dir("logs") / f"launcher_{date.today():%Y%m%d}.log"

    root = logging.getLogger(ROOT_NAME)
    root.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        datefmt="%Y/%m/%d %H:%M:%S")

    try:
        handler = logging.FileHandler(log_file, encoding="utf-8")
    except OSError:
        # ローカル領域に書けない端末でも、ランチャー自体は動かす。
        # ログが無いのは困るが、ログのために起動しないほうがもっと困る
        handler = logging.NullHandler()
    else:
        handler.setFormatter(formatter)
    root.addHandler(handler)

    # **`pythonw.exe` では `sys.stderr` が `None`**。`Start.vbs` は
    # コンソールを出さないために pythonw を使うので、そのまま
    # `StreamHandler()` を付けると1行出すたびに `None.write` で例外に
    # なる (`logging` が握るため表には出ないが、ただの無駄)
    if console is None:
        console = _has_console()
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)

    _configured = True
    _purge_old_logs()


def _has_console() -> bool:
    """標準エラー出力に書けるか。"""
    stream = getattr(sys, "stderr", None)
    return stream is not None and hasattr(stream, "write")


def _purge_old_logs() -> None:
    """古いログを片付ける。失敗しても起動は続ける。"""
    import time

    limit = time.time() - KEEP_DAYS * 86400
    try:
        for path in app_config.local_dir("logs").glob("launcher_*.log"):
            if path.stat().st_mtime < limit:
                path.unlink()
    except OSError:
        pass


def get_logger(name: str) -> logging.Logger:
    """`launcher.<name>` のロガー。"""
    return logging.getLogger(f"{ROOT_NAME}.{name}")
