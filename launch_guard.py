#!/usr/bin/env python3
"""ランチャー自身の多重起動防止 (基盤仕様書 2.4)

ランチャーを二重に起動すると、画面が2枚重なるだけでなく、**どちらが
どのツールを起動したのかが分からなくなる**。片方が止めようとした
ツールを、もう片方が「動いている」と表示し続ける状態になる。

判定は2段構え:

    1. ロックファイルのPIDのプロセスが存在するか
    2. そのプロセスが**このランチャー**か

1だけでは足りない。PIDは使い回されるので、無関係なプロセスが同じ番号を
持っていることがある。そうなると、ランチャーが二度と起動しなくなる。

2は**そのプロセスがいつ起動したか**で見分ける (Windows の API を ctypes で
呼ぶ。外部コマンドは要らない)。ロックを書いたあとで起動したなら別の
プロセス (番号が同じだけ)、前から動いているならロックを書いた本人。
コマンドラインも照合に使うが、**wmic が無く PowerShell も禁じられた端末では
取れない**。起動時刻だけで決められるので、そうした端末でも、電源断などで
残ったロックのせいで起動できなくなることがない。

業務ツール側の多重起動防止は、それぞれのツールが自分で持っている
(4つとも同じ起動基盤を使っている)。ここが見るのはランチャー自身だけ。
"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from launcher import app_config, desktop  # noqa: E402
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
    # 起動はするが、利用者に知らせたいこと (ロックを片付けられなかった、など)
    warning: str = ""
    # 片付けた残りのロック (前のランチャーが**終了の手順を通らずに**終わった:
    # 強制終了・異常終了・電源断)。ふつうに終われば、ロックは必ず消している
    leftover: Optional[LockInfo] = None


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


def remove_lock() -> str:
    """ロックを消す。消せなければその理由、消せた (無かった) なら空。

    Windows では、**読み取り専用**になったファイルは消せない
    (ほかの OS では消せる)。読み取り専用を外してからもう一度試す。
    """
    path = lock_path()
    for attempt in (1, 2):
        try:
            path.unlink()
            log.info("ロックを消しました")
            return ""
        except FileNotFoundError:
            return ""
        except OSError as exc:
            if attempt == 1:
                try:
                    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass
                continue
            log.warning("ロックを消せませんでした: %s", exc)
            return str(exc)
    return ""


def acquire() -> GuardResult:
    """ロックを**取れたら**起動してよい (基盤仕様書 2.4)。

    【なぜ「調べてから書く」ではいけないか】
    `check_existing()` で調べてから `write_lock()` で書くと、その隙間に
    もう1つが割り込める。`Start.vbs` は pythonw で起動するので**押しても
    数秒は何も出ない** ── 利用者はもう一度押す。実際にそうすると、
    2つとも「ロックが無い」と判断して両方起動していた。

    そこで**作成と占有を1回の操作で行う**。`O_CREAT | O_EXCL` は
    「無ければ作る、あれば失敗する」を割り込みなしで行うので、
    同時に来ても必ず片方だけが成功する。
    """
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    leftover: Optional[LockInfo] = None
    for attempt in (1, 2):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            handle = None                     # 誰かが持っている。下で調べる
        except OSError as exc:
            # 書けない場所にある。**起動は止めない** ── ロックのために
            # ランチャーが使えなくなるほうが困る
            log.warning("ロックを作れませんでした (%s): %s", path, exc)
            return GuardResult(True, f"ロックを作れませんでした: {exc}")

        if handle is not None:
            info = build_lock_info()
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(asdict(info), ensure_ascii=False,
                                        indent=2))
            log.info("ロックを取りました: %s (pid=%s)", path, info.pid)
            return GuardResult(True, "ロックを取りました", leftover=leftover)

        # すでに誰かが持っている。中身を見て、生きているかを判断する
        verdict = _inspect_existing()
        leftover = verdict.leftover or leftover
        if not verdict.should_start or verdict.warning:
            # 片付けられないロックなら、取り直しても同じ。**起動は止めない**
            # ── 「ほかのランチャーが起動したようです」と誤って断ると、
            # 消せないロック1つでランチャーがずっと使えなくなる
            return verdict
        if attempt == 1:
            # 死んだロックを片付けた。もう一度だけ取りにいく
            # (その隙に別のランチャーが取っていれば、次で断られる)
            log.info("%s。取り直します", verdict.reason)
            continue
        return GuardResult(False,
                           "ロックを取れませんでした。"
                           "ほかのランチャーが起動したようです")
    return GuardResult(False, "ロックを取れませんでした")


# 書き込み途中のロックを読み切るまで待つ上限 (秒)。
# 作ってから書くまではごく短いので、これで十分すぎる
WRITE_SETTLE_SEC = 1.0


def _inspect_existing() -> GuardResult:
    """すでにあるロックの持ち主を調べる。片付けたなら should_start=True。"""
    info = _read_lock_settled()
    if info is None:
        # ここまで待っても読めない。**本当に壊れている**ので捨ててよい
        log.warning("壊れたロックを片付けます: %s", lock_path())
        return _discard("壊れたロックを片付けました")

    if info.pid == os.getpid():
        return GuardResult(True, "自分自身のロックです")

    verdict = judge_owner(info)
    if verdict.should_start:
        return replace(_discard(verdict.reason), leftover=info)
    return verdict


def _discard(reason: str) -> GuardResult:
    """残っていたロックを片付ける。**片付けられなくても起動は止めない。**"""
    problem = remove_lock()
    if not problem:
        return GuardResult(True, reason)
    return GuardResult(
        True, f"{reason}が、消せませんでした ({problem})",
        warning=(f"前回のロック ({lock_path()}) を消せませんでした。\n{problem}\n\n"
                 "ランチャーは起動しますが、二重に起動していないかを確かめられません。"
                 "\nこのファイルを手で消すか、読み取り専用を外してください。"))


# ロックを書いた時刻と、プロセスの起動時刻の比べの余裕 (秒)。
# 起動してからロックを書くまでは数秒かかるので、逆向きにだけ余裕を見る
PID_REUSE_MARGIN_SEC = 2.0


def judge_owner(info: LockInfo) -> GuardResult:
    """ロックの持ち主がまだ動いているランチャーか。

    should_start=True なら、そのロックは残りもの (片付けてよい)。
    **外部コマンドに頼らない判断を先にする** (起動時刻・実行ファイル)。
    """
    import process_manager

    if not process_manager._is_alive(info.pid):
        return GuardResult(True, f"残っていたロックを片付けました (pid={info.pid})")

    started = desktop.process_started_at(info.pid)
    if started is not None and started > info.started_at + PID_REUSE_MARGIN_SEC:
        # ロックを書いたあとで起動した = 番号が同じだけの別のプロセス
        return GuardResult(True, f"残っていたロックを片付けました (pid={info.pid} は"
                                 "ロックのあとで起動した別のプロセス)")

    command = process_manager.process_command_line(info.pid)
    if command:
        if _looks_like_launcher(command, info):
            return GuardResult(False,
                               f"すでに起動しています (pid={info.pid} / "
                               f"{info.started_text})", info)
        return GuardResult(True, f"pid={info.pid} は別のプロセスでした")

    # コマンドラインが取れない (wmic も PowerShell も使えない端末)
    if started is not None:
        # ロックより前から動いている = **ロックを書いた本人** (動いている
        # プロセスどうしで同じ番号は使われない)
        return GuardResult(False,
                           f"すでに起動しています (pid={info.pid} / "
                           f"{info.started_text})", info)
    image = desktop.process_image(info.pid)
    if image and not Path(image).name.lower().startswith("python"):
        return GuardResult(True, f"pid={info.pid} は別のプロセスでした "
                                 f"({Path(image).name})")
    # 中身を確かめられない。**起動を止めるほうに倒す** ── 本当に
    # 動いているランチャーを2つにするより、起動しないほうがよい
    return GuardResult(False,
                       f"pid={info.pid} が何かを確かめられませんでした。"
                       f"動いていなければ {lock_path()} を削除してください",
                       info)


def _read_lock_settled() -> Optional[LockInfo]:
    """ロックが書き終わるのを待ってから読む。

    **作った直後は中身が空。** `O_CREAT|O_EXCL` で場所を押さえてから
    書き込むので、そのあいだに読むと「壊れている」ように見える。
    そこで片付けてしまうと、**押さえたはずのロックが消えて、
    もう1つが起動できてしまう** (実際にそうなっていた)。

    書き手はすぐ終わるので、少し待って読み直せば足りる。
    """
    deadline = time.monotonic() + WRITE_SETTLE_SEC
    while True:
        info = read_lock()
        if info is not None:
            return info
        if not os.path.exists(lock_path()):
            return None                       # 持ち主が自分で片付けた
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def check_existing() -> GuardResult:
    """すでにランチャーが動いていないか**調べるだけ** (診断用)。

    起動の判断には使わない ── 調べてから書くまでの隙間に割り込まれる。
    起動するときは `acquire()` を使うこと。
    """
    info = read_lock()
    if info is None:
        return GuardResult(True, "ロックがありません")

    if info.pid == os.getpid():
        return GuardResult(True, "自分自身のロックです")

    # `acquire()` と**同じ判断**を使う (片方だけ直した状態を作らない)
    verdict = judge_owner(info)
    if verdict.should_start:
        return replace(_discard(verdict.reason), leftover=info)
    return verdict


def _looks_like_launcher(command: str, info: LockInfo) -> bool:
    """そのコマンドラインがこのランチャーのものか。"""
    import process_manager

    haystack = process_manager._normalize_path(command)
    needle = process_manager._normalize_path(info.app_root or str(APP_ROOT))
    return bool(needle) and needle in haystack
