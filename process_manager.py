#!/usr/bin/env python3
"""対象ツールの確認と安全な停止 (要件定義書 §10 / 基盤仕様書 2.8)

**ランチャーが起動したツールだけを止める。** 同じPCで動く別のPythonアプリを
巻き添えにしないことが、このモジュールの唯一の目的。

要件定義書 §10 と §21 が禁じているのは1点:

    Pythonプロセス名だけを指定して一括終了しない

そこで順序を決める。上から順に試し、**穏やかな方法から**使う:

    1. そのツール自身の `stop.bat` に任せる  … いちばん確実。相手の作法で止まる
    2. `POST /api/shutdown` で正常終了を頼む … `stop.bat` が無いとき
    3. 記録したPIDで止める                   … 応答しないときだけ
       ただし落とす前に、そのPIDが**本当にそのツールか**を確かめる

自分の窓を出すアプリ (Tauri などの exe) は、まず**窓を閉じてもらう**
(×ボタンと同じ)。アプリが「保存しますか」を出したら、そこで待つ ──
落とさずに利用者へ返し、強制終了するかを選んでもらう。

3で確かめるのは、PIDが使い回されるから。記録した時点では日報だったPIDが、
止めるころには無関係なプロセスになっていることがある。コマンドラインに
そのツールの置き場所が入っていることまで見て、取れないときは**止めない**
(誤って別のアプリを落とすより、止まらないほうが害が小さい)。

使い方 (`stop.bat` から呼ばれる):

    python process_manager.py            いま動いているツールを止める
    python process_manager.py --status   状態を見るだけ
    python process_manager.py --force    実行中の処理を中断してでも止める
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from launcher import (app_config, desktop, fileprobe, health,  # noqa: E402
                      runtime_state, tool_entries)
from launcher.logging_utils import get_logger  # noqa: E402
from launcher.runtime_state import RunningTool  # noqa: E402

log = get_logger("process_manager")

# 正常終了を頼んだあと、実際に落ちるのを待つ上限 (秒)
GRACEFUL_WAIT_SEC = 20.0
# stop.bat が 0 以外で終わったあと、止まったかを確かめる短い待ち (秒)
STOP_DECLINED_CHECK_SEC = 2.0
# PIDで止めたあと、消えるのを待つ上限 (秒)
TERMINATE_WAIT_SEC = 8.0
# ブラウザー画面が閉じるのを待つ上限 (秒)。後始末があるので少し長め
BROWSER_WAIT_SEC = 10.0
# 外部コマンド (tasklist / taskkill / wmic) の待ち時間
COMMAND_TIMEOUT_SEC = 8.0
# アプリの窓に「閉じて」と頼んだあと、終わるのを待つ上限 (秒)。
# 過ぎても終わらなければ、保存の確認などを出していると見て利用者に返す
APP_CLOSE_WAIT_SEC = 10.0
# 強制終了のときに、先に窓を閉じてもらう猶予 (秒)
APP_CLOSE_WAIT_FORCE_SEC = 3.0

# Windowsで子プロセスのコンソールを出さないための旗。
# 他のOSでは 0 になり、`creationflags=0` は何もしないのと同じ
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class StopResult:
    """止められたか、どうやって止めたか。"""

    app_id: str
    display_name: str = ""
    stopped: bool = False
    # どの手で止まったか。ログと診断のために残す
    method: str = ""
    message: str = ""
    # 実行中で止めなかったときの、その処理の名前 (基盤仕様書 2.8)
    busy_jobs: list[str] = field(default_factory=list)
    # ブラウザー画面を閉じられたか (要件定義書 §8.3)。
    # 閉じられない形で開いていたときは False のまま
    browser_closed: bool = False

    @property
    def busy(self) -> bool:
        return bool(self.busy_jobs)

    def __str__(self) -> str:
        mark = "済" if self.stopped else "--"
        name = self.display_name or self.app_id
        return f"[{mark}] {name}: {self.message}"


# ------------------------------------------------------------------
# 状態
# ------------------------------------------------------------------
def status(running: Optional[RunningTool] = None) -> Optional[dict]:
    """そのツールが動いているか。動いていれば `/api/health` の中身。

    **記録があること = 動いていること ではない。** 端末を強制終了した
    あとには、動いていないツールの記録だけが残る。最終判断は応答で行う。
    """
    if running is None:
        running = runtime_state.read()
    if running is None:
        return None

    if running.watches_window:
        # Web サーバーを持たないアプリ。プロセスで見る
        if not app_alive(running):
            return None
        payload = {"app_id": running.app_id,
                   "display_name": running.display_name}
    else:
        payload = health.probe(running.health_url)
        if not health.is_tool(payload, running.app_id):
            return None
        payload = dict(payload or {})
    payload["_record"] = {
        "pid": running.pid, "launch_pid": running.launch_pid,
        "port": running.port, "started": running.started_text,
        "start_command": running.start_command,
    }
    return payload


def is_running(running: RunningTool) -> bool:
    """そのツールが動いているか。

    **ツールが起動確認の入口 (`launcher_check.bat` など) を用意していれば、
    それだけで決める** (ランチャー連携 §10。ランチャーは推測しない)。

    無ければ、ブラウザー画面のツールは `/api/health` で見る。アプリの窓の
    ツールは**窓を持つプロセス (exe) が生きているか**を先に見る ── 窓を
    閉じればアプリは終わる。中に Web サーバーを持つアプリは、さらに応答も見る。
    """
    entries = tool_entries.for_record(running)
    if entries.has_check:
        return tool_entries.check(entries).alive
    alive = app_alive(running)
    if alive is False:
        return False
    if not running.health_url:
        return bool(alive)
    return health.is_tool(health.probe(running.health_url), running.app_id)


def app_exe(running: RunningTool) -> str:
    """アプリの窓のツールの exe。exe で起動していなければ空。"""
    path = (running.start_command or "").strip().strip('"')
    return path if path.lower().endswith(".exe") else ""


def app_folder(running: RunningTool) -> str:
    """そのツールの置き場所 (ふつうは起動ファイルのフォルダー)。"""
    exe = app_exe(running)
    for text in (running.work_dir, running.app_root,
                 str(Path(exe).parent) if exe else ""):
        if (text or "").strip():
            return text.strip()
    return ""


def app_pids(running: RunningTool) -> set[int]:
    """アプリのプロセス一式。

    **起動した exe がすぐ終わる作り** (本体を別に起こして戻り値 0 で終わる)
    があるので、記録したPIDだけでは足りない。ツールのフォルダーから起動した
    プロセスと、その子・孫をまとめて見る (`desktop.related_pids`)。記録した
    PIDは、**いまもその exe を実行しているときだけ**使う (PIDは使い回される)。
    """
    exe = app_exe(running)
    if not running.is_app or not exe:
        return set()
    roots = {running.window_pid} if (running.window_pid and
                                     desktop.runs_exe(running.window_pid, exe)) else set()
    return desktop.related_pids(app_folder(running), roots)


def app_alive(running: RunningTool) -> Optional[bool]:
    """アプリが動いているか (プロセス一式のどれかが生きているか)。

    アプリの窓のツールでない・exe でないときは `None` (分からない)。
    """
    if not running.is_app or not app_exe(running):
        return None
    return bool(app_pids(running))


# ------------------------------------------------------------------
# 停止
# ------------------------------------------------------------------
def stop(running: RunningTool, *, force: bool = False,
         timeout: float = GRACEFUL_WAIT_SEC,
         on_backend: Optional[Callable[[], None]] = None) -> StopResult:
    """1つのツールを止める。上に書いた順序どおりに試す。

    `on_backend` は、画面を閉じ終えて**バックエンドを止めにかかる直前**に
    呼ぶ。画面に「〜を終了しています」を、終わってからではなく
    始めるときに出すため。
    """
    result = StopResult(app_id=running.app_id,
                        display_name=running.display_name)
    log.info("停止開始: %s (force=%s)", running.summary(), force)

    # **先に画面を閉じる** (要件定義書 §8.3 の処理順序)。
    # バックエンドを先に落とすと、閉じるまでのあいだ利用者の画面に
    # 「接続できません」が出る
    result.browser_closed = close_browser(running)
    if on_backend is not None:
        on_backend()

    # **ツールが終了の入口 (`launcher_stop.bat` など) を用意していれば、
    # それに任せる** (ランチャー連携 §3.5・§3.6)。ランチャーの止め方
    # (停止要求・窓を閉じる・PID) は、利用者が強制終了を選んだときだけ続ける
    entries = tool_entries.for_record(running)
    if entries.stop:
        outcome = _stop_by_entry(running, entries, timeout=timeout, force=force)
        outcome.browser_closed = result.browser_closed
        if outcome.stopped or not force:
            return outcome
        log.warning("%s。強制終了を選ばれたので、ランチャーの止め方で続けます",
                    outcome.message)

    # アプリの窓のツールは、起動確認 (/api/health) に答えなくても**窓が
    # 生きていれば動いている**。以前はここで「応答しない」とみなして PID で
    # 止めにいっていた (設定のポートがブラウザー版のもので、exe は待ち受け
    # ない場合。python-web-tools の報告)
    if not is_running(running) and not (running.is_app and app_alive(running)):
        # 応答しない。プロセスだけ残っていないか確かめてから片付ける
        return _handle_unresponsive(running, result, force=force)

    method = running.stop_method or "auto"
    attempts: list[str] = []
    if method == "auto":
        attempts = ["stop_bat", "shutdown_api", "pid"]
        if running.is_app:
            # 自分の窓を出すアプリは、まず窓を閉じてもらう (×ボタンと同じ)
            attempts.insert(0, "close_window")
    else:
        # 指定されたやり方で駄目なら、最後はPIDに落とす。
        # 「止まらないまま放置」がいちばん困る
        attempts = [method] if method == "pid" else [method, "pid"]

    # 窓に「閉じて」と頼んだか。頼んだあとは stop.bat・停止要求を使わず PID へ
    # 進む ── デスクトップ版の stop.bat は窓を前に出すために exe を起動する
    # ので、窓が閉じかけだと**新しく起動してしまう** (coil-packing-tools・
    # all-tools の報告)
    asked_window = False
    for attempt in attempts:
        if asked_window and attempt in ("stop_bat", "shutdown_api"):
            log.info("窓に閉じるよう頼んだあとなので、%s は使わずに次へ: %s",
                     attempt, running.app_id)
            continue
        if attempt == "stop_bat":
            outcome = _stop_by_bat(running, force=force, timeout=timeout)
        elif attempt == "close_window":
            outcome = _stop_by_closing(running, force=force)
            asked_window = outcome is not None
        elif attempt == "shutdown_api":
            outcome = _stop_by_api(running, force=force, timeout=timeout)
        else:
            outcome = _stop_by_pid(running, force=force)

        if outcome is None:
            continue                        # その手は使えなかった。次へ
        # 画面を閉じたかどうかは、止め方の結果にも引き継ぐ。落とすと
        # 呼び出し側が「閉じた画面」の後始末をしない
        outcome.browser_closed = result.browser_closed
        if outcome.busy and not force:
            # 実行中の処理がある。**止めずに知らせる** (基盤仕様書 2.8)。
            # 中断してよいかは利用者が決める
            log.info("実行中の処理があるため止めませんでした: %s", outcome.busy_jobs)
            return outcome
        if outcome.busy:
            # 強制終了を選ばれたのに、まだ断られた。次の手へ
            result.message = outcome.message
            continue
        if outcome.stopped:
            log.info("停止完了: %s (%s)", running.app_id, outcome.method)
            return outcome
        result.message = outcome.message    # 理由は最後のものを残す

    if not result.message:
        result.message = "止める方法がありませんでした"
    log.warning("停止できませんでした: %s — %s", running.app_id, result.message)
    return result


def _handle_unresponsive(running: RunningTool, result: StopResult, *,
                         force: bool) -> StopResult:
    """応答しないツールの後片付け。

    2つの場合がある。プロセスごと消えているなら記録を消すだけでよい。
    プロセスは生きているのに応答しないなら、PIDで止めにいく
    (ただし照合してから)。
    """
    if any(pid and _is_alive(pid) for pid in (running.pid, running.launch_pid)):
        # プロセスは生きているのに応答しない。照合してから止めにいく
        outcome = _stop_by_pid(running, force=force)
        if outcome is not None:
            return outcome

    result.stopped = True
    result.method = "already-gone"
    result.message = "動いていませんでした(残っていた記録を片付けました)"
    log.info("%s は動いていませんでした", running.app_id)
    return result


# --- 0. ツールが用意した終了の入口 ---------------------------------
def _stop_by_entry(running: RunningTool, entries: "tool_entries.Entries", *,
                   timeout: float, force: bool = False) -> StopResult:
    """ツールの終了の入口を実行し、止まったことを確かめる。"""
    result = StopResult(app_id=running.app_id,
                        display_name=running.display_name)
    name = Path(entries.stop).name
    log.info("ツールの終了の入口に任せます: %s", entries.stop)
    ok, message = tool_entries.run_stop(entries, timeout=max(timeout, 10.0),
                                        force=force)
    if not ok:
        # ツールが断った (0 以外)。**待たずにすぐ知らせる**
        stopped = False
    elif entries.has_check:
        stopped = _wait_stopped(running, timeout)
    elif running.health_url or app_alive(running) is not None:
        stopped = _wait_stopped(running, timeout)
    else:
        # 確かめる手がかりが無い。**ツールの答え (終了コード 0) を信じる**
        stopped = ok
    result.method = name
    if stopped:
        result.stopped = True
        result.message = "正常に終了しました"
    else:
        result.message = (message if not ok
                          else f"{name} を実行しましたが、まだ動いています")
        # **ツールが止まらなかった (断った) ことを、そのまま利用者に返す。**
        # 実行中の処理があるときと同じく、強制終了するかは利用者が決める
        result.busy_jobs = [result.message]
    return result


# --- 1. そのツール自身の stop.bat ---------------------------------
def resolve_stop_bat(running: RunningTool) -> Optional[Path]:
    """使える `stop.bat` を探す。

    設定されていればそれを使う。設定が無くても、**`start.bat` の隣の
    `stop.bat`** を既定として拾う ── 4つの業務ツールはどれも同じ起動
    基盤で、`start.bat` と `stop.bat` が並んでいる。設定項目を1つ
    減らせるうえ、相手の作法どおりに止められる。
    """
    configured = (running.stop_command or "").strip().strip('"')
    if configured:
        path = Path(configured)
        return path if fileprobe.is_file(path) else None

    start = (running.start_command or "").strip().strip('"')
    if not start:
        return None
    # 起動ファイルの隣。**古い置き場所**でつながらなければ、ここは飛ばして
    # 次の手 (停止要求・PID) へ回る (`fileprobe` は例外を出さない)
    candidate = Path(start).parent / "stop.bat"
    return candidate if fileprobe.is_file(candidate) else None


def _stop_by_bat(running: RunningTool, *, force: bool,
                 timeout: float) -> Optional[StopResult]:
    """相手の `stop.bat` に任せる。使えなければ `None` を返して次へ。"""
    script = resolve_stop_bat(running)
    if script is None:
        return None

    result = StopResult(app_id=running.app_id,
                        display_name=running.display_name)
    command = [str(script)]
    if force:
        # 4つの業務ツールの `stop.bat` は引数をそのまま
        # `process_manager.py` へ渡す。中断してよいときだけ付ける
        command.append("--force")
    log.info("stop.bat に任せます: %s", " ".join(command))

    try:
        completed = subprocess.run(
            command,
            cwd=running.work_dir or str(script.parent),
            stdin=subprocess.DEVNULL,      # BAT末尾の `pause` で固まらせない
            capture_output=True, text=True, errors="replace",
            timeout=timeout, creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        result.message = f"stop.bat が {timeout:.0f}秒 で終わりませんでした"
        log.warning("%s", result.message)
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        result.message = f"stop.bat を実行できませんでした: {exc}"
        log.warning("%s", result.message)
        return result

    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        log.info("stop.bat の戻り値は %s でした: %s",
                 completed.returncode, output.strip()[:400])
        # **0 以外 = ツールが止めなかった** (実行中の処理がある、など)。
        # 以前は戻り値を見ずに止まるのを待ち (30秒)、次の手へ進んでいた。
        # 少しだけ確かめ、まだ動いていれば、理由を添えてすぐ返す。強制終了
        # するかは利用者が決める (そのときは --force で呼び直す)
        if _wait_stopped(running, STOP_DECLINED_CHECK_SEC):
            result.stopped = True
            result.method = "stop.bat"
            result.message = "正常に終了しました"
            return result
        reason = _first_lines(output) or f"終了コード {completed.returncode}"
        result.message = f"{script.name} が止めませんでした: {reason}"
        result.busy_jobs = [reason]
        return result

    if _wait_stopped(running, timeout):
        result.stopped = True
        result.method = "stop.bat"
        result.message = "正常に終了しました"
        return result

    result.message = "stop.bat を実行しましたが、まだ応答しています"
    return result


def _first_lines(output: str, count: int = 2, limit: int = 160) -> str:
    """出力のはじめの数行 (理由が先に出る stop.bat が多い。最後は案内の決まり文句)。"""
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    return " / ".join(lines[:count])[:limit]


def _wait_stopped(running: RunningTool, timeout: float) -> bool:
    """止まったことを確かめる。

    ブラウザー画面のツールは `/api/health` が答えなくなるまで。アプリの
    窓のツールは**窓を持つプロセスが消えるまで**も待つ (Web サーバーを
    持たなければ、それだけが手がかり)。
    """
    entries = tool_entries.for_record(running)
    if entries.has_check:
        # ツールの起動確認の入口が「動いていない」と言うまで
        return tool_entries.wait_until(entries, want_alive=False, timeout=timeout)
    deadline = time.monotonic() + timeout
    while True:
        alive = app_alive(running)
        server = (bool(running.health_url)
                  and health.is_tool(health.probe(running.health_url),
                                     running.app_id))
        if not alive and not server:
            if alive is None and not running.health_url:
                return False                  # 何も確かめられない
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)


# --- 0. アプリの窓を閉じてもらう ------------------------------------
def _stop_by_closing(running: RunningTool, *,
                     force: bool) -> Optional[StopResult]:
    """アプリの窓に「閉じて」と頼む (×ボタンと同じ)。使えなければ `None`。

    アプリは保存の確認を出したり、後片付けをしてから終われる。待っても
    終わらなければ、**確認を出して待っているもの**とみなし、落とさずに
    「実行中」として返す (強制終了するかは利用者が決める)。強制のときは
    少しだけ待って、次の手 (PID で止める) へ回す。
    """
    pids = app_pids(running)
    if not pids:
        return None
    result = StopResult(app_id=running.app_id,
                        display_name=running.display_name)

    asked = desktop.close_windows(pids)
    if asked is None:
        # 窓の無い環境 (Windows 以外)。終了の要求で代える
        if not any(_terminate(pid, force=False) for pid in sorted(pids)):
            return None
    elif asked == 0:
        log.info("%s は窓を出していないので、ほかの手で止めます",
                 running.app_id)
        return None
    else:
        # 確認を出すなら、それが利用者に見えるように前へ
        desktop.bring_to_front(pids)
    log.info("アプリの窓を閉じるよう頼みました: %s (pid=%s)", running.app_id,
             sorted(pids))

    wait = APP_CLOSE_WAIT_FORCE_SEC if force else APP_CLOSE_WAIT_SEC
    if _wait_stopped(running, wait):
        result.stopped = True
        result.method = "close-window"
        result.message = "アプリの窓を閉じて終了しました"
        return result
    if force:
        result.message = "窓を閉じても終わらないので、強制終了します"
        return result
    result.busy_jobs = ["終了の確認 (アプリの窓を見てください)"]
    result.message = ("窓を閉じるよう頼みましたが、まだ終わっていません。"
                      "アプリが保存の確認などを出していないか見てください")
    return result


# --- 2. POST /api/shutdown ----------------------------------------
def shutdown_url(running: RunningTool) -> str:
    """停止要求の宛先。起動確認URLと同じ場所に置かれている前提。"""
    if running.port <= 0:
        return ""
    return f"http://127.0.0.1:{running.port}/api/shutdown"


def find_shutdown_token(running: RunningTool) -> str:
    """相手の停止要求トークンを探す。取れなければ空文字。

    4つの業務ツールは、自分のローカル領域へ
    `runtime/*.lock` を書き、その中に停止用のトークンを入れている。
    どのフォルダかは相手の `config/app.json` の `local_dir_name` が持つ。

        相手の置き場所 (`/api/health` の `app_root`)
          → config/app.json の local_dir_name
          → %LOCALAPPDATA%\\<それ>\\runtime\\*.lock
          → ポートが一致する行の token

    **best effort。** 取れなくても止められる (`stop.bat` かPIDへ回る)。
    ここで失敗しても記録だけ残して静かに進む。
    """
    root = (running.app_root or running.work_dir or "").strip()
    if not root:
        return ""
    try:
        config = json.loads(
            (Path(root) / "config" / "app.json").read_text(encoding="utf-8"))
        local_name = str(config.get("local_dir_name", "")).strip()
    except (OSError, ValueError) as exc:
        log.debug("相手の設定を読めませんでした (%s): %s", root, exc)
        return ""
    if not local_name:
        return ""

    base = os.environ.get("LOCALAPPDATA")
    local_root = (Path(base) / local_name if base
                  else Path.home() / ".local" / "share" / local_name)
    try:
        locks = sorted((local_root / "runtime").glob("*.lock"))
    except OSError:
        return ""

    for lock in locks:
        try:
            info = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if int(info.get("port", 0)) == running.port:
            return str(info.get("token", ""))
    return ""


def _stop_by_api(running: RunningTool, *, force: bool,
                 timeout: float) -> Optional[StopResult]:
    """`POST /api/shutdown` で正常終了を頼む。"""
    url = shutdown_url(running)
    if not url:
        return None

    result = StopResult(app_id=running.app_id,
                        display_name=running.display_name)
    headers = {"Content-Type": "application/json"}
    token = find_shutdown_token(running)
    if token:
        headers["X-Tool-Token"] = token
    body = json.dumps({"force": force}).encode("utf-8")
    log.info("停止要求を送ります: %s (token=%s)", url, "あり" if token else "なし")

    try:
        with health.local_request(url, timeout=5, data=body, method="POST",
                                  headers=headers) as res:
            payload = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            # 実行中の長時間処理がある (基盤仕様書 2.8)
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (OSError, ValueError):
                payload = {}
            result.busy_jobs = list(payload.get("running") or ["(不明)"])
            result.message = ("実行中の処理があります: "
                              + "、".join(result.busy_jobs))
            return result
        result.message = f"停止要求が断られました (HTTP {exc.code})"
        log.info("%s", result.message)
        return result
    except (urllib.error.URLError, OSError, ValueError) as exc:
        result.message = f"停止要求を送れませんでした: {exc}"
        log.info("%s", result.message)
        return result

    if not payload.get("stopped"):
        result.message = str(payload.get("message") or "停止要求は受理されませんでした")
        return result

    if _wait_stopped(running, timeout):
        result.stopped = True
        result.method = "shutdown-api"
        result.message = "正常に終了しました"
        return result

    result.message = "停止要求は受理されましたが、まだ応答しています"
    return result


# --- 3. PIDで止める ------------------------------------------------
def _stop_by_pid(running: RunningTool, *, force: bool) -> Optional[StopResult]:
    """記録したPIDで止める。**落とす前に本当にそのツールかを確かめる。**"""
    result = StopResult(app_id=running.app_id,
                        display_name=running.display_name)

    if running.is_app and app_exe(running):
        # 起動した exe が終わっていても、ツールのフォルダーのプロセスを止める
        targets = sorted(app_pids(running))
    else:
        targets = [pid for pid in (running.pid, running.launch_pid)
                   if pid and _is_alive(pid)]
    if not targets:
        result.stopped = True
        result.method = "already-gone"
        result.message = "すでに終了していました"
        return result

    stopped_any = False
    refused = False
    for pid in targets:
        verdict = verify_process(pid, running)
        if not verdict.ok:
            result.message = (
                f"PID {pid} はこのツールではないようなので止めません "
                f"({verdict.reason})")
            log.warning("PID %s の照合に失敗したため停止しません: %s",
                        pid, verdict.reason)
            continue

        log.info("PID %s を停止します (%s)", pid, verdict.reason)
        if _terminate(pid, force=False) and _wait_pid_gone(pid, TERMINATE_WAIT_SEC):
            stopped_any = True
            continue
        if not force:
            result.message = (f"PID {pid} が終了要求に応じません。"
                              "中断してでも止めるには --force を付けてください")
            refused = True
            continue
        if _terminate(pid, force=True) and _wait_pid_gone(pid, TERMINATE_WAIT_SEC):
            stopped_any = True
            result.method = "kill"
        else:
            result.message = f"PID {pid} を止められませんでした"

    if stopped_any:
        result.stopped = True
        result.method = result.method or "terminate"
        result.message = ("強制終了しました" if result.method == "kill"
                          else "終了しました")
    elif refused and running.is_app:
        # 窓の無いアプリ (通知領域だけ) は、頼んでも終わらないことがある。
        # **落とさずに返し**、強制終了するかは利用者に選んでもらう
        result.busy_jobs = ["終了の要求に応じない"]
    return result


@dataclass
class Verdict:
    """そのPIDを止めてよいか、と理由。"""

    ok: bool
    reason: str


def verify_process(pid: int, running: RunningTool) -> Verdict:
    """そのPIDが本当にそのツールか (要件定義書 §10「対象アプリを特定してから終了する」)。

    コマンドラインに**そのツールの置き場所**が入っていることを確かめる。
    別のフォルダにある同じツールや、無関係なPythonを巻き添えにしない。

    コマンドラインが取れない環境では**止めない**。誤って別のアプリを
    落とすより、止まらないほうが害が小さい ── 止まらなければ利用者は
    気づいて手で対処できるが、他人の処理を落とすと気づかれないまま
    データが失われる。
    """
    exe = app_exe(running)
    if running.is_app and exe and desktop.runs_exe(pid, exe):
        # 実行ファイルのフルパスで確かめられる (外部コマンドを使わない)
        return Verdict(True, f"実行ファイルが {exe} です")
    if running.is_app and exe and pid in app_pids(running):
        return Verdict(True, "ツールのフォルダーから起動したプロセスです")

    command = process_command_line(pid)
    if not command:
        return Verdict(False, "コマンドラインを取得できませんでした")

    haystack = _normalize_path(command)
    for marker in (running.start_command, running.app_root, running.work_dir):
        needle = _normalize_path(marker)
        if not _is_specific_enough(needle):
            # 短すぎる印では照合にならない。`C:\` のような値を許すと
            # **どのプロセスにも一致してしまい、照合が素通りになる**
            continue
        if needle in haystack:
            return Verdict(True, f"コマンドラインが {marker} を含みます")

    return Verdict(False, "コマンドラインにこのツールの場所が含まれません")


# 照合に使ってよい印の短さの下限。`C:/` や `/opt` のような値では
# 「そのツールである」ことの根拠にならない
MIN_MARKER_LENGTH = 8


def _is_specific_enough(needle: str) -> bool:
    """その印だけでツールを言い当てられるか。

    区切りを1つ以上含み、ある程度の長さがあることを求める。
    ここを緩めると照合が形だけになり、無関係なプロセスを落とす
    (このモジュールが避けたい唯一のこと)。
    """
    return len(needle) >= MIN_MARKER_LENGTH and "/" in needle.strip("/")


def _normalize_path(text: str) -> str:
    """パスの比較用に均す。

    Windowsは大文字小文字を区別せず、`\\` と `/` が混ざる。
    `start.bat` のパスはそのままだと末尾のファイル名まで含むので、
    ここでは単純な文字列として突き合わせる。
    """
    if not text:
        return ""
    return text.strip().strip('"').replace("\\", "/").rstrip("/").lower()


# ------------------------------------------------------------------
# ブラウザー画面 (要件定義書 §8.3)
# ------------------------------------------------------------------
def close_browser(running: RunningTool) -> bool:
    """ランチャーが開いた画面だけを閉じる。閉じたら True。

    **利用者が別に開いている Google やメールには手が届かない。**
    閉じにいくのは、ランチャーが専用プロファイルで起こしたプロセスだけ
    ── そのプロファイルの道がコマンドラインに入っていることを確かめて
    から落とす。確かめられなければ落とさない。

    プロファイルは利用者のふだんのブラウザーとは別物なので、ここで
    落としても通常のタブは1つも閉じない。照合はその上での二重の守り。
    """
    if not running.browser_managed:
        # 既定ブラウザーへ渡しただけ。閉じる手がかりが無い
        return False
    if not _is_alive(running.browser_pid):
        log.info("画面はすでに閉じられていました (pid=%s)", running.browser_pid)
        return True

    from launcher import browser

    if browser.is_spawned(running.browser_pid):
        # **このランチャーが自分で起こした画面。** 手がかりを持っているので
        # コマンドラインの照合は要らない。これで `wmic` も PowerShell も
        # 使えない端末 (Windows 11 24H2 以降は wmic が既定で無い、工場の
        # 端末では PowerShell を禁じていることがある) でも閉じられる
        return _close_window(running.browser_pid)

    # 手がかりが無い = 前のランチャーが開いた画面。外から照合するしかない
    command = process_command_line(running.browser_pid)
    if not command:
        log.warning("画面 pid=%s の中身を確かめられないので閉じません",
                    running.browser_pid)
        return False

    needle = _normalize_path(running.browser_profile)
    if not _is_specific_enough(needle) or needle not in _normalize_path(command):
        # 別のブラウザーのPIDを掴んでいる。落とすと利用者のタブを
        # 巻き添えにするので、**触らない**
        log.warning("画面 pid=%s は専用プロファイルではないので閉じません",
                    running.browser_pid)
        return False

    return _close_window(running.browser_pid)


def _close_window(pid: int) -> bool:
    """照合の済んだ画面を閉じる。"""
    log.info("画面を閉じます: pid=%s", pid)
    # まず穏やかに頼む。Windowsの `taskkill /T` は WM_CLOSE を送るので、
    # ブラウザーは後始末をしてから終われる
    if _terminate(pid, force=False) and _wait_pid_gone(pid, BROWSER_WAIT_SEC):
        return True
    if _terminate(pid, force=True) and _wait_pid_gone(pid, BROWSER_WAIT_SEC):
        log.info("画面を強制的に閉じました: pid=%s", pid)
        return True
    log.warning("画面を閉じられませんでした: pid=%s", pid)
    return False


def is_browser_open(running: RunningTool) -> bool:
    """ランチャーが開いた画面がまだ出ているか。

    利用者が手で閉じたことに気づくために使う。**バックエンドの生死とは
    別に見る** (要件定義書 §11)。
    """
    if not running.browser_managed:
        return False
    return _is_alive(running.browser_pid)


# ------------------------------------------------------------------
# プロセスの生死と中身
# ------------------------------------------------------------------
def _is_alive(pid: int) -> bool:
    """そのPIDのプロセスが存在するか。中身までは見ない。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _is_alive_windows(pid)
    try:
        os.kill(pid, 0)                    # シグナル0は存在確認だけ
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                        # 別ユーザーのプロセス = 生きている
    # **終了済みでも、親が引き取るまでPIDは残る (ゾンビ)。**
    # ランチャーは `start.bat` を自分の子として起こすので、止めたあとに
    # ここへ来る。ゾンビを「生きている」と答えると、止まったものを
    # 止まっていないと報告し続けることになる
    return not _is_zombie(pid)


def is_pid_alive(pid: int) -> bool:
    """そのPIDのプロセスが残っているか。

    障害記録の手がかり ── 「応答しない」が「落ちた」のか「固まった」
    のかで、次に調べるところが違う。
    """
    return _is_alive(pid)


def _is_zombie(pid: int) -> bool:
    """終了済みで、親に引き取られるのを待っているだけの状態か。

    Linux の `/proc` を見る。無い環境 (macOS など) では判断できないので
    「ゾンビではない」に倒す ── その場合は `os.kill` の結果がそのまま
    答えになり、これまでどおりの振る舞いに戻る。
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # `comm` に空白や括弧が入りうるので、最後の `)` の後ろから読む
    tail = stat.rpartition(")")[2].split()
    return bool(tail) and tail[0] == "Z"


def _is_alive_windows(pid: int) -> bool:
    """Windows では `tasklist` で確認する。

    `OpenProcess` を ctypes で叩く手もあるが、権限やハンドルの後始末を
    誤ると別の不具合を招く。止めるときに数回だけの判定なので、
    外部コマンドの数十msは問題にならない。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, errors="replace",
            timeout=COMMAND_TIMEOUT_SEC, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("tasklist を実行できませんでした: %s", exc)
        return True                        # 分からないときは「生きている」に倒す
    return f'"{pid}"' in out.stdout


def find_process_by_marker(marker: str) -> int:
    """コマンドラインにその印を含む生きたプロセスを1つ探す。無ければ 0。

    記録を失ったあとに、**すでに開いている画面を見つけ直す**ために使う。
    印は専用プロファイルの道のように、そのプロセスだけが持つ長い文字列
    でなければならない (短い印はどれにでも当たる)。
    """
    needle = _normalize_path(marker)
    if not _is_specific_enough(needle):
        return 0
    for pid, command in _running_processes():
        if needle in _normalize_path(command):
            return pid
    return 0


def _running_processes():
    """(PID, コマンドライン) を順に返す。取れなければ何も返さない。"""
    if os.name == "nt":
        yield from _running_processes_windows()
        return
    try:
        entries = sorted(Path("/proc").iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue                          # 消えた / 見られない
        if raw:
            yield int(entry.name), raw.replace(b"\0", b" ").decode(
                "utf-8", "replace").strip()


def _running_processes_windows():
    """Windows のプロセス一覧。**1回の呼び出しでまとめて取る。**

    1つずつ問い合わせると、プロセスの数だけ外部コマンドを起こすことに
    なる。探すのは画面を開く前の1回だけなので、まとめて取って絞る。
    """
    script = ("Get-CimInstance Win32_Process | "
              "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, errors="replace",
            timeout=COMMAND_TIMEOUT_SEC * 2, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("プロセス一覧を取れませんでした: %s", exc)
        return
    for line in out.stdout.splitlines():
        pid, _, command = line.partition("\t")
        if pid.strip().isdigit() and command.strip():
            yield int(pid.strip()), command.strip()


def inspection_status() -> tuple[bool, str]:
    """この端末で、プロセスのコマンドラインを取れるか (診断用)。

    取れない端末では、ランチャーが**前回開いた画面**を閉じられず、
    記録を失ったときに**既に開いている画面を探し出せない**。止めるとき
    も PID での停止ができない (stop.bat と停止要求は使える)。
    配る前に分かるよう、`start_debug.bat --check` に出す。
    """
    command = process_command_line(os.getpid())
    if command:
        if os.name != "nt":
            return True, "/proc から取得"
        return True, "取得できます"
    return False, ("取得できません (wmic も PowerShell も使えない端末です)")


def process_command_line(pid: int) -> str:
    """そのPIDが何を実行しているか。**停止前の照合に使う**。

    取れなければ空文字 (呼び出し側は安全側に倒して止めない)。
    """
    if pid <= 0:
        return ""
    if os.name == "nt":
        return _command_line_windows(pid)
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def _command_line_windows(pid: int) -> str:
    """Windowsのコマンドライン。`wmic` → PowerShell の順に試す。

    `wmic` は新しいWindowsでは既定で入っていない (非推奨になった)。
    そこだけで諦めると、**照合できない=止めない**になってランチャーが
    ツールを終了できなくなるので、CIM を叩く道も用意する。
    """
    try:
        out = subprocess.run(
            ["wmic", "process", "where", f"ProcessId={pid}", "get",
             "CommandLine", "/format:list"],
            capture_output=True, text=True, errors="replace",
            timeout=COMMAND_TIMEOUT_SEC, creationflags=NO_WINDOW)
        for line in out.stdout.splitlines():
            if line.startswith("CommandLine="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return value
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("wmic を実行できませんでした: %s", exc)

    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\")"
             ".CommandLine"],
            capture_output=True, text=True, errors="replace",
            timeout=COMMAND_TIMEOUT_SEC, creationflags=NO_WINDOW)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("プロセスのコマンドラインを取得できませんでした: %s", exc)
        return ""


def _terminate(pid: int, *, force: bool) -> bool:
    """1つのプロセス (と、その子・孫) を止める。要求を出せたかを返す。

    子まで止めるのは、`start.bat` から起こした `cmd.exe` の下にPythonが
    ぶら下がっているため ── 親だけ落とすと、業務ツール本体が残る。

    **ランチャー自身とその祖先は決して止めない** (`desktop.protected_pids`)。
    以前は `taskkill /T` に子をたどらせていた。Windows の「親のPID」は
    番号のまま残って使い回されるので、たとえば Start.vbs の wscript の
    番号でツールが起動すると、**ランチャーがツールの子に見えて一緒に
    落とされた** (ツールを起動・停止したらランチャーが閉じる)。子は
    起動時刻で確かめてから、こちらで1つずつ指定する。
    """
    protected = desktop.protected_pids()
    if pid in protected:
        log.error("ランチャー自身 (またはそれを起こしたプロセス) は止めません: pid=%s",
                  pid)
        return False
    targets = sorted((desktop.process_tree(pid) | {pid}) - protected)
    if os.name == "nt":
        command = ["taskkill"]
        for target in targets:
            command += ["/PID", str(target)]
        if force:
            command.append("/F")
        log.info("止めます: %s%s", targets, " (強制)" if force else "")
        try:
            subprocess.run(command, capture_output=True, text=True,
                           errors="replace", timeout=COMMAND_TIMEOUT_SEC,
                           creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("taskkill を実行できませんでした: %s", exc)
            return False
        return True

    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return True                        # もう居ない = 目的は達している
    except OSError as exc:
        log.warning("PID %s を止められませんでした: %s", pid, exc)
        return False
    return True


def _wait_pid_gone(pid: int, timeout: float) -> bool:
    started = time.monotonic()
    while _is_alive(pid):
        if time.monotonic() - started >= timeout:
            return False
        time.sleep(0.2)
    return True


# ------------------------------------------------------------------
# CLI (`stop.bat` から呼ばれる)
# ------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="ランチャーが起動した業務ツールを安全に停止する")
    parser.add_argument("--status", action="store_true",
                        help="状態を見るだけで止めない")
    parser.add_argument("--force", action="store_true",
                        help="実行中の処理を中断してでも止める")
    args = parser.parse_args(argv)

    from launcher.logging_utils import configure_logging
    configure_logging()

    records = runtime_state.read_all()
    if not records:
        print("ランチャーから起動した業務ツールはありません。")
        return 0

    # ツールは同時に複数動く。**記録にあるものを1つずつ**扱う
    code = 0
    for running in records.values():
        if args.status:
            payload = status(running)
            if payload is None:
                print(f"{running.summary()} は応答していません"
                      "(記録だけが残っています)。")
                continue
            print(f"{running.summary()} は動いています。")
            for key in ("app_id", "display_name", "version", "app_root", "ready"):
                if key in payload:
                    print(f"  {key}: {payload[key]}")
            continue

        result = stop(running, force=args.force)
        if running.is_app:
            pass                              # 画面はアプリ自身の窓
        elif running.browser_managed:
            print("[済] 画面を閉じました" if result.browser_closed
                  else "[--] 画面を閉じられませんでした")
        elif running.browser_pid or running.url:
            print("[--] 画面はふだんのブラウザーで開いています。手で閉じてください")
        print(result)
        if result.busy:
            print("  中断して止めるには --force を付けてください。")
            code = 1
        elif result.stopped:
            runtime_state.remove(running.app_id)
        else:
            code = 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
