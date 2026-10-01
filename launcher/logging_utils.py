"""ログ出力 (要件定義書 §16 / 基盤仕様書 2.6)

「起動しない」「切り替わらない」を、利用者の説明だけに頼らず調べられる
ようにするための記録。最低限これを残す:

    ランチャー起動日時 / アプリID / 起動開始 / 起動完了 / PID /
    ポート / 起動失敗 / 停止開始 / 停止完了 / 終了理由 / エラー内容

出力先は［設定］の「ログの出力先」(無ければユーザー別ローカル領域の
`logs`、基盤仕様書 2.7)。決め方は `trace.destination()` を参照。
出来事の一覧 (CSV) と障害記録も同じ場所に置く (`trace`)。
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

from . import app_config

_configured = False
_file_handler: "logging.Handler | None" = None

# ログ名の親。`get_logger()` はこの下にぶら下げる
ROOT_NAME = "launcher"

# 何日分残すか。起動のたびに1行ずつ増える程度なので大きくは要らないが、
# 「先週から切り替わらない」の類は数日さかのぼれないと追えない
KEEP_DAYS = 30

# 片付けの対象。**業務ツールの出力も含める** ── こちらは日付で
# 分かれておらず追記され続けるので、放っておくと無制限に増える
LOG_PATTERNS = ("launcher_*.log", "tool_*.out.log")


def configure_logging(*, console: bool | None = None) -> None:
    """起動時に一度だけ呼ぶ。二重呼び出しは無害 (冪等)。"""
    global _configured
    if _configured:
        return

    app_config.ensure_local_dirs()
    root = logging.getLogger(ROOT_NAME)
    root.setLevel(logging.DEBUG)
    _attach_file_handler(root)

    # **`pythonw.exe` では `sys.stderr` が `None`**。`Start.vbs` は
    # コンソールを出さないために pythonw を使うので、そのまま
    # `StreamHandler()` を付けると1行出すたびに `None.write` で例外に
    # なる (`logging` が握るため表には出ないが、ただの無駄)
    if console is None:
        console = _has_console()
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(_formatter())
        # **画面には INFO 以上だけ。** 細かい記録はファイルに残す。
        # 診断起動 (`start_debug.bat --check`) は利用者が読むものなので、
        # 起動確認の応答なし1件ずつのような DEBUG が混ざると、
        # 肝心のバージョンや設定の行が埋もれる
        stream.setLevel(logging.INFO)
        root.addHandler(stream)

    _configured = True
    _purge_old_logs()


def _formatter() -> logging.Formatter:
    return logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        datefmt="%Y/%m/%d %H:%M:%S")


def log_file_path() -> "Path":
    """いまのランチャーのログ (今日の分)。"""
    from .trace import destination

    return destination().path / f"launcher_{date.today():%Y%m%d}.log"


def _attach_file_handler(root: logging.Logger) -> None:
    global _file_handler
    from .trace import destination

    folder = destination(refresh=True).path
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(
            folder / f"launcher_{date.today():%Y%m%d}.log", encoding="utf-8")
    except OSError:
        # 書けない端末でも、ランチャー自体は動かす。ログが無いのは
        # 困るが、ログのために起動しないほうがもっと困る
        handler = logging.NullHandler()
    else:
        handler.setFormatter(_formatter())
    root.addHandler(handler)
    _file_handler = handler


def reconfigure() -> None:
    """ログの出力先が変わったとき (［設定］で保存したとき) に書き先を移す。"""
    global _file_handler
    if not _configured:
        return
    root = logging.getLogger(ROOT_NAME)
    if _file_handler is not None:
        root.removeHandler(_file_handler)
        _file_handler.close()
    _attach_file_handler(root)
    _purge_old_logs()


def _has_console() -> bool:
    """標準エラー出力に書けるか。"""
    stream = getattr(sys, "stderr", None)
    return stream is not None and hasattr(stream, "write")


def _purge_old_logs() -> None:
    """古いログを片付ける。失敗しても起動は続ける。"""
    import time

    from .trace import destination, purge_old

    limit = time.time() - KEEP_DAYS * 86400
    # **自分の端末のフォルダーの中だけ**片付ける (共有フォルダーでも、
    # 出力先には端末名のフォルダーが付いているので、ほかの端末に触らない)
    for folder in {app_config.local_dir("logs"), destination().path}:
        try:
            for pattern in LOG_PATTERNS:
                for path in folder.glob(pattern):
                    if path.stat().st_mtime < limit:
                        path.unlink()
        except OSError:
            continue
    try:
        purge_old()
    except OSError:
        pass


def rotate_if_large(path, *, max_bytes: int, keep: int = 2) -> None:
    """大きくなったファイルを退避する。

    業務ツールの出力 (`tool_<アプリID>.out.log`) は日付で分かれず、
    動いているあいだ追記され続ける。**日数で消すだけでは、開いたまま
    長く使う端末を守れない** ── 1つのファイルが際限なく育つ。

    そこで上限を超えたら `.1`、`.2` と退けて、新しいファイルから書き直す。
    `logging.handlers.RotatingFileHandler` を使わないのは、書いている
    のが `logging` ではなく**子プロセス自身**だから (こちらは開くときに
    退けるだけ)。
    """
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
    except OSError:
        return

    try:
        # 古いほうから押し出す。keep=2 なら .2 を捨てて .1 → .2、本体 → .1
        oldest = path.with_suffix(path.suffix + f".{keep}")
        if oldest.exists():
            oldest.unlink()
        for index in range(keep - 1, 0, -1):
            source = path.with_suffix(path.suffix + f".{index}")
            if source.exists():
                source.rename(path.with_suffix(path.suffix + f".{index + 1}"))
        path.rename(path.with_suffix(path.suffix + ".1"))
    except OSError:
        # 退けられなくても起動は続ける。**ログのために起動しないほうが困る**
        pass


def get_logger(name: str) -> logging.Logger:
    """`launcher.<name>` のロガー。"""
    return logging.getLogger(f"{ROOT_NAME}.{name}")
