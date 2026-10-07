#!/usr/bin/env python3
"""ツールの起動・停止 (要件定義書 §7 / §8 / §9)

ランチャーの中身はほぼこれ1つ。**業務ロジックは持たない** ──
各ツールの起動方法は、それぞれのリポジトリの `start.bat` に集約する
(要件定義書 §12.2 / §21)。ここが持つのは順序と状態だけ:

    ボタンが押された
       ↓
    そのツールが動いている?  → はい: 画面を前に出すだけ (二重起動しない §9)
       ↓ いいえ
    start.bat を実行する          ※ ほかのツールは止めない (同時に使える)
       ↓
    /api/health が応答するまで待つ  ← **ここを飛ばさない** (§7.2)
       ↓
    ブラウザー画面を開く
       ↓
    動作中：<ツール名>、<ツール名>

画面とバックエンドは別々に扱う (§11)。利用者が画面だけ手で閉じると、
ツールは「誰も見ていない」と判断して自分で終わる。ランチャーはそれに
気づいて、そのツールを一覧から外す。

**自分の窓を出すアプリ** (Tauri などの exe) は流れが少し違う:

    exe を実行する                ※ すでに動いていれば前に出すだけ
       ↓
    窓が出るまで待つ              ← Web サーバーを持つなら /api/health
       ↓
    ブラウザーは開かない (アプリの窓が画面)
       ↓
    窓を閉じればアプリは終わる → 一覧から外す。戻り値が 0 以外なら異常終了

起動・停止・思わぬ停止は、**あとから追えるように** `trace` へ残す
(出来事の一覧と、失敗したときの障害記録)。障害記録には、ランチャーが
確かめられた「なぜ」を並べておく。

画面 (tkinter) はこのモジュールを読み込むが、**このモジュールは画面を
読み込まない**。起動・停止の判断だけを持つので、画面が無い環境でも
試験できる (基盤仕様書 2.5「起動制御と業務ロジックを分ける」)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import process_manager  # noqa: E402
from launcher import (app_config, browser, desktop, fileprobe,  # noqa: E402
                      health, logging_utils, runtime_state, tool_registry,
                      trace)
from launcher.logging_utils import get_logger  # noqa: E402
from launcher.runtime_state import RunningTool  # noqa: E402
from launcher.tool_registry import Tool  # noqa: E402

log = get_logger("app_manager")

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Ctrl+C を業務ツールへ伝えない。診断起動 (コンソールあり) でランチャーを
# 止めたときに、動いている業務ツールまで落とさないため
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


class State(str, Enum):
    """ランチャーバーに出す状態 (要件定義書 §5.2)。"""

    IDLE = "idle"                # 何も動いていない
    STARTING = "starting"        # 起動しています...
    RUNNING = "running"          # 動作中：<ツール名>
    STOPPING = "stopping"        # 終了しています...
    ERROR = "error"              # 起動できませんでした


# 起動・停止のどの段にいるか。**進み具合の窓** (`startup_progress`)
# が、どこまで済んだかを描くのに使う
PHASE_CLOSE_BROWSER = "close_browser"    # 止めるツールの画面を閉じる
PHASE_STOP_TOOL = "stop_tool"            # ツールを終了する
PHASE_SPAWN = "spawn"                    # 起動ファイルを実行する
PHASE_WAIT = "wait"                      # 起動の完了を待つ (/api/health)
PHASE_OPEN_BROWSER = "open_browser"      # 画面を開く

# アプリの窓が出るのを待つ上限 (秒)。起動の上限より短くする ──
# 通知領域にだけ入るアプリは窓を出さないので、長く待たせない
APP_WINDOW_WAIT_SEC = 30.0
# 窓を見られない環境 (Windows 以外) で、起動できたとみなすまでの時間
APP_SETTLE_SEC = 0.8
# 起動した exe がすぐ終わったあと、本体 (別に起こされたプロセス) が見つかる
# まで待つ時間 (秒)。**起動用の exe が本体を起こして戻り値 0 で終わる**作りがある
STUB_GRACE_SEC = 5.0
# 設定のポートで答えないとき、ツールが本当に待ち受けているポートを探す間隔 (秒)
DISCOVER_EVERY_SEC = 2.0
# ブラウザーで使うツールが**自分の窓**を出していないか見る間隔 (秒)
WINDOW_LOOK_SEC = 1.0

# `shutdown(stop_tools=False)` で手放した exe のプロセス。**落とさない** ──
# exe はツールそのものなので、引き取ろうとして落とすとツールが消える
_DETACHED: list[subprocess.Popen] = []


@dataclass
class Status:
    """画面に渡す1枚。**画面はこれだけを見て描く**。

    `state` / `message` は**いま起きたこと** (起動した・止めた・失敗した)。
    どのツールが動いているかは `running_ids` などの一覧で持つ ──
    ツールは同時に複数動くので、1つの状態では表せない。
    """

    state: State = State.IDLE
    # その知らせが**どのツールのことか**。全体の知らせなら空
    app_id: str = ""
    display_name: str = ""
    message: str = ""
    # 利用者が次に何をすればよいか。案内があるときだけ入る
    detail: str = ""
    elapsed: float = 0.0
    # バックエンドが応答しているか (基盤仕様書 2.9)。
    # ブラウザーを閉じたことと、ツールが落ちたことは別物
    responding: bool = False
    # ランチャーが開いた画面がまだ出ているか (要件定義書 §8.3 / §11)
    browser_open: bool = False
    # その画面をランチャーが閉じられるか。既定ブラウザーへ渡しただけの
    # ときやツールが自分で開いたときは False
    browser_managed: bool = False
    tool_version: str = ""
    # 起動・停止のどの段か (`PHASE_*`)。忙しくないときは空
    phase: str = ""
    # ツールが `/api/health` で返した準備の段階 (「アプリを準備中」など)
    stage: str = ""
    # 起動を待つ上限 (秒)。経過と並べて出す
    timeout: float = 0.0
    # --- その時点の全体の様子 (ボタンの色分けに使う) ---
    running_ids: tuple[str, ...] = ()
    starting_ids: tuple[str, ...] = ()
    stopping_ids: tuple[str, ...] = ()
    # 最後に選ばれたツール。バーで少し強めに目立たせる
    focus_id: str = ""
    # 障害記録を残したときの場所。［詳細］から開けるように
    incident: str = ""
    # そのツールの画面の出し方。"browser" / "app" (進み具合の窓が使う)
    ui_mode: str = ""

    @property
    def busy(self) -> bool:
        return self.state in (State.STARTING, State.STOPPING)


StatusCallback = Callable[[Status], None]


class ToolManager:
    """動いているツールを**同時に複数**持つ。

    以前は「AからBへ切り替えるときAを止める」1つずつの作りだった。
    ところがツールが自分の画面をふだんのブラウザーに開く形 (ランチャー
    が閉じられない画面) だと、止めたツールの画面が残り、どれも
    「バックエンドに接続できません」になる。

    そこで**選んだツールを起こすだけで、ほかは止めない**形にした。
    各ツールは「誰も見ていなければ終了する」見張りを持っているので、
    画面を閉じればそのツールは自分で終わる。ランチャーはそれに気づいて
    ボタンの色を戻す。
    """

    def __init__(self, on_status: Optional[StatusCallback] = None) -> None:
        self._on_status = on_status
        self._status = Status()
        self._lock = threading.RLock()
        # 動いているツール。アプリIDごと
        self._running: dict[str, RunningTool] = runtime_state.read_all()
        # `start.bat` で起こしたプロセス (cmd.exe)。引き取るために持つ
        self._processes: dict[str, subprocess.Popen] = {}
        # 応答が無かった回数。**1回で「終了した」と断じない** ──
        # スリープ復帰直後や、重い処理でHTTP応答が遅れたときに
        # 動いているツールを落ちた扱いにしてしまう
        self._failures: dict[str, int] = {}
        # いま起こしている・止めている最中のツール。**同じものを二重に
        # 起こさない印**。ボタンを素早く2回押すと、どちらの要求も
        # 「まだ動いていない」と見て、両方が `start.bat` を実行してしまう
        self._starting: set[str] = set()
        self._stopping: set[str] = set()
        # ボタンを押してから、その処理 (動いているかの確かめ・起動) が終わる
        # まで。**このあいだにもう一度押されても、確かめ直さない** ──
        # 起こしかけのツールを「外で動いていたもの」と取り違えて引き継ぎ、
        # 準備ができる前に画面を開いてしまう
        self._selecting: set[str] = set()
        # 起動を待っている最中に「やめる」と言われたツール
        self._cancelled: set[str] = set()
        # 画面が開いていたか (前回の見回り)。変わったときだけ知らせる
        self._browser_seen: dict[str, bool] = {}
        # 最後に選ばれたツール
        self._focus = ""
        # 起こしている最中のツールの操作 (後追いの記録)。「やめる」が
        # 押されたとき、同じ操作IDで残すため
        self._ops: dict[str, trace.Operation] = {}
        # 止めようとして断られたツール (実行中の処理・保存の確認)。
        # 画面が［強制終了］を出すのに使う
        self._refused: set[str] = set()
        # 窓は出ているのに、中の Web サーバーが答えないアプリ。
        # 障害記録を1回だけ書くための印
        self._unhealthy: set[str] = set()

    # --------------------------------------------------------------
    # 状態
    # --------------------------------------------------------------
    def set_status_callback(self, callback: Optional[StatusCallback]) -> None:
        """状態の受け取り先を差し替える。

        画面は自分ができてから受け取りたいので、`ToolManager` を作った
        あとで登録する。**別スレッドから呼ばれる**ことに注意 ──
        tkinter を直接触らず、キューへ置くだけにすること。
        """
        self._on_status = callback

    @property
    def status(self) -> Status:
        return self._status

    @property
    def running(self) -> dict[str, RunningTool]:
        """動いているツール (写し)。"""
        with self._lock:
            return dict(self._running)

    @property
    def current(self) -> Optional[RunningTool]:
        """最後に選ばれた (無ければ最後に起動した) 動いているツール。"""
        with self._lock:
            if self._focus in self._running:
                return self._running[self._focus]
            if not self._running:
                return None
            return max(self._running.values(), key=lambda r: r.started_at)

    @property
    def starting_ids(self) -> list[str]:
        """起動している最中のツール (まだ動いている一覧には入っていない)。"""
        with self._lock:
            return sorted(a for a in self._starting if a not in self._running)

    def is_busy(self, app_id: str) -> bool:
        with self._lock:
            return app_id in self._starting or app_id in self._stopping

    def stop_refused(self, app_id: str) -> bool:
        """止めようとして断られたか (実行中の処理・終了の確認)。

        断られたツールには、画面が［強制終了］を出す。
        """
        with self._lock:
            return app_id in self._refused and app_id in self._running

    def _emit(self, status: Status) -> None:
        self._status = status
        if self._on_status is not None:
            try:
                self._on_status(status)
            except Exception:                 # noqa: BLE001 - 画面の都合で処理を止めない
                log.exception("状態の通知に失敗しました")

    def _set(self, state: State, message: str, who=None, *, detail: str = "",
             elapsed: float = 0.0, responding: bool = False,
             browser_open: Optional[bool] = None, phase: str = "",
             stage: str = "", timeout: float = 0.0,
             incident: str = "") -> None:
        """知らせを出す。`who` は `Tool` か `RunningTool` (どのツールのことか)。"""
        app_id = getattr(who, "app_id", "") if who is not None else ""
        name = getattr(who, "display_name", "") if who is not None else ""
        ui_mode = (getattr(who, "resolved_ui_mode", "")
                   or getattr(who, "ui_mode", "")) if who is not None else ""
        with self._lock:
            running = self._running.get(app_id)
            running_ids = tuple(self._running)
            starting = set(self._starting)
            stopping = set(self._stopping)
        if state not in (State.STARTING, State.STOPPING) and app_id:
            # **終わったという知らせ**は、そのツールを「最中」に数えない。
            # 起動・停止の印は知らせを出したあとで外すので、ここで外して
            # おかないと、動いているのに「…」のままボタンに残る
            starting.discard(app_id)
            stopping.discard(app_id)
        starting_ids = tuple(sorted(starting))
        stopping_ids = tuple(sorted(stopping))
        if browser_open is None:
            browser_open = (running is not None
                            and process_manager.is_browser_open(running))
        self._emit(Status(
            state=state, app_id=app_id, display_name=name or app_id,
            message=message, detail=detail, elapsed=elapsed,
            responding=responding, browser_open=browser_open,
            browser_managed=bool(running and running.browser_managed),
            phase=phase, stage=stage, timeout=timeout,
            running_ids=running_ids, starting_ids=starting_ids,
            stopping_ids=stopping_ids, focus_id=self._focus,
            incident=incident, ui_mode=ui_mode))

    def notify(self, detail: str) -> None:
        """全体への案内を出す (起動ファイルを確かめられない、など)。

        いまの案内 (引き継いだツールの説明など) があれば、その後ろに足す。
        """
        current = self._status.detail
        merged = f"{current}\n\n{detail}" if current else detail
        with self._lock:
            responding = bool(self._running)
        self._set(self._settled_state(), self.summary(), detail=merged,
                  responding=responding)

    def summary(self) -> str:
        """「動作中：日報、看板」。何も動いていなければ「起動していません」。"""
        with self._lock:
            names = [r.display_name or r.app_id for r in self._running.values()]
        if not names:
            return "起動していません"
        return "動作中：" + "、".join(names)

    def _settled_state(self) -> State:
        """何も起きていないときの状態。動いているツールがあれば RUNNING。"""
        with self._lock:
            return State.RUNNING if self._running else State.IDLE

    # --------------------------------------------------------------
    # 起動していたものを引き継ぐ
    # --------------------------------------------------------------
    def adopt_running(self) -> list[RunningTool]:
        """すでに動いているツールを**すべて**見つけて、引き継ぐ。

        ランチャーを再起動したとき (記録は残っている) と、利用者が
        `Start.vbs` から直接ツールを起動していたとき。どちらも「動いて
        いるのに、ランチャーは何も知らない」状態で、そのままボタンを
        押すと二重起動になる (要件定義書 §9)。
        """
        recorded = runtime_state.read_all()
        tools = tool_registry.all_tools()
        payloads = _probe_all(tools)

        adopted: dict[str, RunningTool] = {}
        outside: list[str] = []
        for tool in tools:
            if tool.watches_window:
                # Web サーバーを持たないアプリ。プロセスで探す
                running = recorded.get(tool.app_id)
                if running is None or not process_manager.is_running(running):
                    running = _find_running_app(tool)
                if running is not None:
                    adopted[tool.app_id] = running
                continue
            payload = payloads.get(tool.app_id)
            if not health.is_tool(payload, tool.app_id):
                # 設定のポートでは答えない。それでも**ツールのプロセスが動いて
                # いれば**引き継ぐ (起動用の exe が本体を別に起こす作り・
                # ポートの設定ちがい)。引き継がないと、押したとき2つ目を起こす
                if tool.is_app or recorded.get(tool.app_id) is not None:
                    found = self._find_existing(tool)
                    if found is not None:
                        adopted[tool.app_id] = found
                continue
            running = recorded.get(tool.app_id)
            if running is None:
                running = _running_from_health(tool, payload, launch_pid=0)
                if tool.is_app:
                    # 窓を持つ exe を探しておく (前に出す・閉じるのに要る)
                    found = _find_app_pid(tool)
                    running.launch_pid = found
                else:
                    # **この画面はランチャーが開いたものではない。** 閉じる
                    # 手がかりが無い
                    outside.append(tool.display_name)
            adopted[tool.app_id] = running
        # 登録から消えたツールでも、記録があって応答していれば引き継ぐ
        for app_id, running in recorded.items():
            if app_id not in adopted and app_id not in payloads \
                    and process_manager.is_running(running):
                adopted[app_id] = running

        with self._lock:
            self._running = adopted
            self._failures = {}
        runtime_state.replace_all(adopted)
        for app_id, running in adopted.items():
            log.info("動いているツールを引き継ぎました: %s", running.summary())
            outside_tool = app_id not in recorded
            trace.event("引き継ぎ", trace.INFO, tool=running,
                        cause=("ランチャーの外で起動されていた" if outside_tool
                               else "前回のランチャーが起動したもの"))

        detail = ""
        if outside:
            detail = ("次のツールはランチャーの外で起動されています: "
                      + "、".join(outside) + "\n画面はランチャーからは閉じられ"
                      "ないので、止めたあとは手で閉じてください。")
        self._set(self._settled_state(), self.summary(), detail=detail,
                  responding=bool(adopted))
        return list(adopted.values())

    # --------------------------------------------------------------
    # ボタンが押された
    # --------------------------------------------------------------
    def select(self, app_id: str) -> None:
        """ツールのボタンが押されたときの入口。すぐ戻る (処理は別スレッド)。

        **ほかのツールは止めない。** 動いていなければ起こし、動いて
        いれば画面を前に出す。
        """
        tool = tool_registry.get(app_id)
        if tool is None:
            self._set(State.ERROR, "登録されていないツールです",
                      detail=f"アプリID: {app_id}")
            return
        self._focus = app_id
        op = trace.operation("ボタン", tool)
        threading.Thread(target=self._run_select, args=(tool, op),
                         name=f"select-{tool.app_id}", daemon=True).start()

    def _run_select(self, tool: Tool,
                    op: Optional[trace.Operation] = None) -> None:
        op = op or trace.operation("ボタン", tool)
        try:
            self._select_blocking(tool, op)
        except Exception as exc:                  # noqa: BLE001 - 画面に出して継続
            log.exception("起動処理で予期しない失敗: %s", tool.app_id)
            cause = f"{type(exc).__name__}: {exc}"
            path = trace.incident(
                f"{tool.display_name}の起動処理で想定外の失敗", tool=tool, op=op,
                whys=[f"ランチャーの中で想定外の例外が起きた ({cause})"],
                observed=[("起動ファイル", tool.start_command or "(未設定)"),
                          ("作業フォルダー", tool.resolved_work_dir or "-")],
                hints=["ランチャーの不具合の可能性があります。"
                       "この記録を開発担当へ渡してください。"],
                tool_log=tool_log_path(tool.app_id), exc=exc)
            trace.event("想定外の例外", trace.FAILED, tool=tool, op=op,
                        cause=cause, incident=path)
            self._set(State.ERROR, f"{tool.display_name}を起動できませんでした",
                      tool, detail=f"{exc}\n" + _records_note(path),
                      incident=path)

    def _select_blocking(self, tool: Tool,
                         op: Optional[trace.Operation] = None) -> None:
        """起動の本体。試験からはこちらを直接呼ぶ。"""
        op = op or trace.operation("ボタン", tool)
        self._focus = tool.app_id
        with self._lock:
            busy = tool.app_id in self._selecting or tool.app_id in self._starting
            if not busy:
                self._selecting.add(tool.app_id)
        if busy:
            # 起動の最中にもう一度押された。2つ目は起こさず、出ている窓を前へ
            self._show_starting(tool, op)
            return
        try:
            self._select_unlocked(tool, op)
        finally:
            with self._lock:
                self._selecting.discard(tool.app_id)

    def _show_starting(self, tool: Tool, op: trace.Operation) -> None:
        """起動の最中に、そのツールのボタンがもう一度押された。

        **2つ目は起こさない。** ツールの窓がもう出ていれば前に出す (起動の
        確かめが窓に気づく少し前)。まだ窓が無ければ、進み具合の窓をバーが
        出し直す (［隠す］で隠していても)。
        """
        with self._lock:
            proc = self._processes.get(tool.app_id)
        folder = _own_folder(tool)
        roots = {proc.pid} if proc is not None else set()
        pids = desktop.related_pids(folder, roots) if (folder or roots) else set()
        brought = (desktop.bring_to_front(pids) if pids else False) \
            or _front_by_title(tool)
        log.info("%s は起動の最中です (窓を前へ=%s)", tool.display_name, brought)
        trace.event("起動中に押された", trace.INFO, tool=tool, op=op,
                    detail="ツールの窓を前に出した" if brought
                    else "まだ窓が無い (起動を続ける)")

    def _select_unlocked(self, tool: Tool, op: trace.Operation) -> None:
        with self._lock:
            running = self._running.get(tool.app_id)
        if running is not None:
            if process_manager.is_running(running):
                self._show(tool, running, op)
                return
            if self._tool_alive(running):
                # 応答しないが、**ツールのプロセスはまだ動いている**。起動し直すと
                # 2つ目になる (ポートの取り合い・同じデータの書き合い)。前に出すだけ
                log.info("%s は動いていますが応答しません。起動し直しません",
                         tool.display_name)
                trace.event("応答なし", trace.WARNING, tool=running, op=op,
                            cause="動いているが応答しないので、起動し直さずに前に出す")
                self._show(tool, running, op)
                return
            # 記録はあるが、プロセスごと無い。落ちている。起動し直す
            log.info("%s は動いていないので起動し直します", tool.display_name)
            trace.event("応答なし", trace.WARNING, tool=running, op=op,
                        cause="動いている記録はあるがプロセスが無いので起動し直す")
            self._forget(tool.app_id)
        # ランチャーの外 (デスクトップのショートカット・前のランチャー) で
        # 起動されていれば、**もう1つ起動しない**。前に出すだけにする
        found = self._find_existing(tool)
        if found is not None:
            with self._lock:
                self._running[tool.app_id] = found
                self._failures[tool.app_id] = 0
            runtime_state.put(found)
            log.info("すでに動いていたツールを引き継ぎます: %s", found.summary())
            trace.event("引き継ぎ", trace.INFO, tool=found, op=op,
                        cause="押したときにはもう動いていた (起動せずに前に出す)")
            self._show(tool, found, op)
            return
        self._start(tool, op)

    def _find_existing(self, tool: Tool) -> Optional[RunningTool]:
        """すでに動いている、そのツール。無ければ None。

        1. 設定のポートの起動確認が、そのツールとして答える
        2. ツールのフォルダーから起動したプロセスがある (起動用の exe が
           本体を別に起こす作りでも見つかる)。待ち受けているポートで
           起動確認が答えれば、そのポートで引き継ぐ
        """
        # 待ち受けが無いのに当たりにいかない (Windows では断られるまで
        # 1〜2 秒かかり、押すたびに待たせる)
        if tool.health_url and desktop.port_listening(tool.port) is not False:
            payload = health.probe(tool.health_url)
            if health.is_tool(payload, tool.app_id):
                running = _running_from_health(tool, payload, launch_pid=0)
                if tool.is_app:
                    running.launch_pid = _find_app_pid(tool)
                return running
        if not tool.start_command.strip():
            return None
        folder = _own_folder(tool)
        pids = desktop.processes_in_folder(folder) if folder else []
        if tool.entry_kind == "exe":
            pids = sorted(set(pids) | set(desktop.find_by_exe(tool.start_command)))
        if not pids:
            return None
        payload = self._discover(tool, desktop.process_tree(pids)) \
            if tool.health_url else None
        if payload is not None:
            running = _running_from_health(tool, payload, launch_pid=0)
            running.launch_pid = _find_app_pid(tool) if tool.is_app else 0
            return running
        if tool.is_app:
            return _running_for_app(tool, _find_app_pid(tool) or pids[0])
        # 応答はしないが、ツールのプロセスは動いている
        running = _running_from_health(tool, {}, launch_pid=0)
        running.pid = pids[0]
        running.confirmed = False
        return running

    def _discover(self, tool: Tool, pids) -> Optional[dict]:
        """**ツールが本当に待ち受けているポート**を探し、起動確認が答えれば
        その応答を返す (`port` と `_health_url` を入れる)。

        設定のポートと、ツールが実際に使うポートが違うことがある (役割ごとに
        ポートが違うツール・設定を直していない)。そのときも、ツールの
        プロセスが待ち受けているポートを当たれば見つかる。
        """
        if not pids:
            return None
        path = tool.health_path or tool_registry.DEFAULT_HEALTH_PATH
        if not path.startswith("/"):
            path = "/" + path
        for host, port in desktop.listening_ports(pids):
            if port == tool.port and tool.health_url:
                continue                      # 設定のポートは当たったあと
            hosts = ["[::1]"] if ":" in host and host not in ("::",) else ["127.0.0.1"]
            if host == "::":
                hosts = ["127.0.0.1", "[::1]"]
            for name in hosts:
                url = f"http://{name}:{port}{path}"
                payload = health.probe(url)
                if health.is_tool(payload, tool.app_id):
                    found = dict(payload)
                    found["port"] = port      # 答えたポートを信じる
                    found["_health_url"] = url
                    log.info("%s は設定のポート %s ではなく %s で答えました",
                             tool.app_id, tool.port, port)
                    return found
        return None

    def _front_tool_window(self, running: RunningTool) -> bool:
        """ツールのプロセス一式が出している窓を前に出せたか。"""
        pids = self._tool_pids(running)
        return bool(pids) and desktop.bring_to_front(pids)

    def _tool_pids(self, running: RunningTool) -> set[int]:
        """そのツールのプロセス一式 (ツールのフォルダーから起動したもの・
        ランチャーが起こしたもの、その子・孫)。"""
        with self._lock:
            proc = self._processes.get(running.app_id)
        folder = _own_folder(running)
        if proc is not None:
            # 起こしたプロセスのハンドルを持っているあいだは、その番号は
            # ほかに使い回されない。終わっていても、子をたどる起点にしてよい
            return desktop.related_pids(folder, {proc.pid})
        if running.is_app:
            return process_manager.app_pids(running) if folder else set()
        return desktop.related_pids(folder) if folder else set()

    def _tool_alive(self, running: RunningTool) -> bool:
        """そのツールのプロセスが、どれか動いているか。"""
        return bool(self._tool_pids(running))

    def _show(self, tool: Tool, running: RunningTool,
              op: Optional[trace.Operation] = None) -> None:
        """動いているツールの画面を出す。**起動し直さない** (§9)。"""
        if running.is_app:
            name = tool.display_name
            pids = self._tool_pids(running)
            brought = (desktop.bring_to_front(pids) if pids else False) \
                or _front_by_title(tool)
            shown = brought or (desktop.has_window(pids) if pids else False)
            trace.event("窓を前へ", trace.OK if brought else trace.INFO,
                        tool=running, op=op,
                        detail="" if brought else "前に出せなかった")
            detail = ""
            if shown is False:
                detail = (f"{name}は動いていますが、窓が見つかりません。\n"
                          "通知領域 (画面右下の ^) に入っていないか見てください。")
            self._set(State.RUNNING, self.summary(), tool, detail=detail,
                      responding=True)
            return
        if process_manager.is_browser_open(running):
            # もう出ている。**もう1枚開かない** ── 同じツールの窓が2つ
            # 並ぶと作業状態を奪い合う。前に出すだけにする
            brought = browser.bring_to_front(running.browser_pid)
            log.info("すでに動いていて画面も出ています: %s (前へ=%s)",
                     tool.display_name, brought)
            trace.event("画面を前へ", trace.OK if brought else trace.INFO,
                        tool=running, op=op,
                        detail="" if brought else "前に出せなかった")
            detail = ""
        elif not running.browser_managed and self._front_tool_window(running):
            # ツールが**自分の窓**を出している (中にブラウザーを持つ exe など)。
            # ランチャーの画面は開かない (開けば画面が2枚)。その窓を前に出す
            log.info("ツールの窓を前に出しました: %s", tool.display_name)
            trace.event("窓を前へ", trace.OK, tool=running, op=op)
            detail = ""
        elif running.browser_managed or tool.suppresses_browser:
            # 利用者が画面だけ手で閉じていた。バックエンドは動いたまま
            # なので、起動し直さず画面だけ開く (§11)
            log.info("画面だけ開き直します: %s", tool.display_name)
            self._open_browser(running, running.url or tool.home_url)
            trace.event("画面を開き直す", trace.INFO, tool=running, op=op)
            detail = ""
        else:
            # 画面はツールが自分で開いている。その窓を探して前に出す
            pids = self._tool_pids(running)
            brought = (desktop.bring_to_front(pids) if pids else False) \
                or _front_by_title(tool)
            trace.event("画面を前へ" if brought else "動作中", trace.INFO,
                        tool=running, op=op,
                        cause="画面はツールがふだんのブラウザーに開いている")
            detail = "" if brought else (
                f"{tool.display_name}は動いています。画面はツールが"
                "ふだんのブラウザーに開いているので、そちらを見てください。")
        self._set(State.RUNNING, self.summary(), tool, detail=detail,
                  responding=True)

    # --------------------------------------------------------------
    # 停止
    # --------------------------------------------------------------
    def stop(self, app_id: str, *, force: bool = False) -> None:
        """1つのツールを止める。すぐ戻る。起動中なら起動をやめる。"""
        with self._lock:
            if app_id in self._starting:
                self._cancelled.add(app_id)
                op = self._ops.get(app_id)
                trace.event("起動をやめる", trace.INFO,
                            tool=tool_registry.get(app_id), op=op,
                            cause="起動を待っているあいだに［止める］が押された")
                return
        threading.Thread(target=self._stop_blocking, args=(app_id,),
                         kwargs={"force": force}, name=f"stop-{app_id}",
                         daemon=True).start()

    def stop_current(self, *, force: bool = False) -> None:
        """最後に選ばれたツールを止める。"""
        running = self.current
        if running is not None:
            self.stop(running.app_id, force=force)

    def stop_all(self, *, force: bool = False) -> None:
        """動いているツールをすべて止める。すぐ戻る。"""
        threading.Thread(target=self._stop_all_blocking, kwargs={"force": force},
                         name="stop-all", daemon=True).start()

    # 起動をやめさせたツールが片付くのを待つ上限 (秒)。起動を待つ見回りは
    # 0.5 秒ごとなのですぐ気づくが、立ち上がりかけていれば止めにいくので少し長め
    CANCEL_WAIT_SEC = 15.0

    def _stop_all_blocking(self, *, force: bool = False) -> bool:
        with self._lock:
            self._cancelled.update(self._starting)
            targets = list(self._running)
        op = trace.operation("すべて停止")
        results = [self._stop_blocking(app_id, force=force, op=op)
                   for app_id in targets]
        return all(results) and self._wait_starts_cancelled()

    def _wait_starts_cancelled(self) -> bool:
        """起動をやめさせたツールが、片付け終わるのを待つ。

        **待たずにランチャーが終わると、片付けの途中で打ち切られる** ──
        起動しかけたツールが、誰も知らないまま残る。
        """
        deadline = time.monotonic() + self.CANCEL_WAIT_SEC
        while True:
            with self._lock:
                left = set(self._starting)
            if not left:
                return True
            if time.monotonic() >= deadline:
                log.warning("起動をやめさせたツールが片付きません: %s",
                            "、".join(sorted(left)))
                return False
            time.sleep(0.1)

    def _stop_blocking(self, app_id: str, *, force: bool = False,
                       op: Optional[trace.Operation] = None) -> bool:
        """1つのツールを止めて、**終了を確認する** (要件定義書 §8.1)。"""
        with self._lock:
            running = self._running.get(app_id)
            if running is None:
                return True
            if app_id in self._stopping:
                return False                  # すでに止めている最中
            self._stopping.add(app_id)
        try:
            return self._stop_locked(running, force=force,
                                     op=op or trace.operation("停止", running))
        finally:
            with self._lock:
                self._stopping.discard(app_id)

    def _stop_locked(self, running: RunningTool, *, force: bool,
                     op: Optional[trace.Operation] = None) -> bool:
        name = running.display_name or running.app_id
        began = time.monotonic()
        trace.event("停止要求", trace.INFO, tool=running, op=op,
                    detail="強制終了" if force else "")
        with self._lock:
            self._refused.discard(running.app_id)
        # 画面を閉じることから始まるので、そう伝える (要件定義書 §8.2)
        if running.is_app:
            self._set(State.STOPPING, f"{name}を閉じています...", running,
                      phase=PHASE_STOP_TOOL)
        elif running.browser_managed and process_manager.is_browser_open(running):
            self._set(State.STOPPING, f"{name}の画面を閉じています...", running,
                      phase=PHASE_CLOSE_BROWSER)
        else:
            self._set(State.STOPPING, f"{name}を終了しています...", running,
                      phase=PHASE_STOP_TOOL)

        def on_backend() -> None:
            self._set(State.STOPPING, f"{name}を終了しています...", running,
                      phase=PHASE_STOP_TOOL)

        # `process_manager.stop` が中で画面を先に閉じてから
        # バックエンドを止める (要件定義書 §8.3 の処理順序)
        result = process_manager.stop(
            running, force=force,
            timeout=float(app_config.ui_setting("stop_timeout_seconds")),
            on_backend=on_backend)
        if result.browser_closed:
            browser.forget(running.browser_pid)

        if result.busy:
            # 実行中の処理がある。**止めずに知らせる** (基盤仕様書 2.8)。
            # 中断してよいかは利用者が決める
            detail = (f"{name}で実行中の処理があります: "
                      + "、".join(result.busy_jobs)
                      + "\n終了するときは［ツール停止］から「強制終了」を"
                        "選んでください")
            if running.is_app:
                detail = (f"{name}は終了の確認を出しているようです。"
                          "アプリの窓で答えてください。\n"
                          "確かめずに終わらせるときは、［ツール停止］から"
                          "「強制終了」を選んでください (保存していない内容は"
                          "失われます)。")
            with self._lock:
                self._refused.add(running.app_id)
            if result.browser_closed:
                detail += ("\n画面は閉じましたが、処理は続いています。"
                           "同じボタンを押すと画面を開き直せます。")
            trace.event("停止を見送り", trace.WARNING, tool=running, op=op,
                        cause="実行中の処理がある: " + "、".join(result.busy_jobs),
                        elapsed=time.monotonic() - began)
            self._set(State.RUNNING, self.summary(), running, detail=detail,
                      responding=True)
            return False

        if not result.stopped:
            path = self._record_stop_failure(running, result, force=force, op=op)
            trace.event("停止失敗", trace.FAILED, tool=running, op=op,
                        cause=result.message, detail=result.method,
                        elapsed=time.monotonic() - began, incident=path)
            self._set(State.ERROR, f"{name}を終了できませんでした", running,
                      detail=f"{result.message}\n" + _records_note(path),
                      incident=path)
            return False

        self._forget(running.app_id)
        log.info("停止完了: %s (%s)", name, result.method)
        trace.event("停止完了", trace.OK, tool=running, op=op,
                    detail=f"止め方: {result.method}"
                    + ("" if result.browser_closed else " / 画面は閉じていない"),
                    elapsed=time.monotonic() - began)
        # **ランチャーが閉じられない画面が残るなら、そう伝える。**
        # 「終了しました」とだけ出ると、残ったタブを見た利用者は
        # 止まっていないのかと思う
        note = _leftover_note(running, name)
        message = f"{name}を終了しました"
        if note:
            message += "（画面は手で閉じてください）"
        self._set(self._settled_state(), message, running, detail=note)
        return True

    def _forget(self, app_id: str) -> None:
        """止まったツールを一覧から外し、`start.bat` の受け皿を引き取る。"""
        self._reap_process(app_id)
        with self._lock:
            self._running.pop(app_id, None)
            self._failures.pop(app_id, None)
            self._browser_seen.pop(app_id, None)
            self._refused.discard(app_id)
            self._unhealthy.discard(app_id)
        runtime_state.remove(app_id)

    # `start.bat` の受け皿が終わるのを待つ上限 (秒)
    REAP_WAIT_SEC = 3.0

    def _reap_process(self, app_id: str) -> None:
        """`start.bat` で起こしたプロセスを引き取る。

        引き取らないと、終了済みのプロセスがゾンビとしてPID表に残り、
        「まだ動いている」と誤って判定される。

        業務ツール本体が止まれば、それを起こした `cmd.exe` もふつうは
        一緒に終わる。**終わらないときは落とす** ── 残しておくと、
        見えないコンソールが起動のたびに1つずつ増えていく。
        自分で起こしたプロセスなので、照合は要らない。
        """
        with self._lock:
            proc = self._processes.pop(app_id, None)
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    proc.wait(timeout=self.REAP_WAIT_SEC)
                except subprocess.TimeoutExpired:
                    log.info("start.bat の受け皿 (pid=%s) が残ったので止めます",
                             proc.pid)
                    proc.kill()
                    proc.wait(timeout=self.REAP_WAIT_SEC)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log.debug("start.bat の受け皿を引き取れませんでした: %s", exc)

    # --------------------------------------------------------------
    # 起動
    # --------------------------------------------------------------
    def _start(self, tool: Tool, op: Optional[trace.Operation] = None) -> None:
        op = op or trace.operation("起動", tool)
        problem = tool_registry.validate_start_command(tool.start_command)
        if not tool.start_command.strip():
            trace.event("起動できない", trace.FAILED, tool=tool, op=op,
                        cause="起動ファイル (start.bat) が設定されていない")
            self._set(State.ERROR, f"{tool.display_name}が設定されていません", tool,
                      detail=("設定画面で start.bat の場所を指定してください。\n"
                              "(ランチャーバーの「設定」ボタン)"))
            return
        if problem:
            trace.event("起動できない", trace.FAILED, tool=tool, op=op,
                        cause=problem, detail=tool.start_command)
            self._set(State.ERROR, f"{tool.display_name}を起動できません", tool,
                      detail=f"{problem}\n設定画面で指定し直してください。")
            return
        problem = tool.ui_problem()
        if problem:
            trace.event("起動できない", trace.FAILED, tool=tool, op=op,
                        cause=problem)
            self._set(State.ERROR, f"{tool.display_name}を起動できません", tool,
                      detail=f"{problem}\n設定画面で直してください。")
            return

        # **同じツールは1つずつ。** 起こしている最中なら、2度目は何もしない
        # ── 2つ目の `start.bat` はポートが空いていないので失敗し、
        # **起動できているのに「起動できませんでした」**と出ることになる
        with self._lock:
            if tool.app_id in self._starting:
                log.info("%s を起こしている最中です", tool.app_id)
                return
            if tool.app_id in self._running:
                log.info("%s はすでに動いています", tool.app_id)
                return
            self._starting.add(tool.app_id)
            self._cancelled.discard(tool.app_id)
            self._ops[tool.app_id] = op

        try:
            self._start_locked(tool, op)
        finally:
            with self._lock:
                self._starting.discard(tool.app_id)
                self._cancelled.discard(tool.app_id)
                self._ops.pop(tool.app_id, None)

    def _start_locked(self, tool: Tool,
                      op: Optional[trace.Operation] = None) -> None:
        """起動の本体。**同じツールでは1つしか走らない**ことが前提。"""
        op = op or trace.operation("起動", tool)
        timeout = float(app_config.ui_setting("start_timeout_seconds"))
        self._set(State.STARTING, f"{tool.display_name}を起動しています...", tool,
                  phase=PHASE_SPAWN, timeout=timeout)
        proc = self._spawn(tool, op)
        if proc is None:
            return
        with self._lock:
            self._processes[tool.app_id] = proc

        started = time.monotonic()
        # BATが先に落ちたことを、時間切れと区別するための入れ物。
        # 「90秒待った末に時間切れ」より「start.bat が3秒で終了した
        # (戻り値 1)」のほうが、次に何を見ればよいか分かる
        early_exit: dict[str, int] = {}
        # 待っているあいだに最後に返った応答。**時間切れの理由を分ける
        # 手がかり** (別のアプリが答えていた・準備が終わらなかった)
        seen: dict[str, dict] = {}

        def should_stop() -> bool:
            if tool.app_id in self._cancelled:
                return True
            code = proc.poll()
            if code is not None and code != 0:
                early_exit["code"] = code
                return True
            return False

        def on_progress(elapsed: float, payload: Optional[dict]) -> None:
            if payload:
                if "payload" not in seen:
                    op.step(f"初めて応答あり ({elapsed:.1f}秒): "
                            f"app_id={payload.get('app_id', '?')}")
                seen["payload"] = payload
            stage = health.stage_text(payload)
            message = f"{tool.display_name}を起動しています..."
            if stage:
                message = f"{tool.display_name}: {stage}"
            self._set(State.STARTING, message, tool, elapsed=elapsed,
                      phase=PHASE_WAIT, stage=stage, timeout=timeout)

        window_note = ""
        how = "health"
        if tool.is_app:
            how, payload, window_note = self._wait_app(tool, proc, timeout,
                                                       early_exit, seen, op)
            if how in ("window", "alive"):
                payload = {}                  # 応答は無いが、起動はしている
            elif how != "health":
                payload = None
        else:
            # 起動を待つあいだも、ツールが**別のポート**で待ち受けていないか
            # 時々見る (設定のポートが違っていても 90 秒待たせない)
            discovered: dict[str, dict] = {}
            # ツールが**自分の窓**を出したか (中にブラウザーを持つ exe など)
            own_window: dict[str, set] = {}
            next_look = [time.monotonic() + DISCOVER_EVERY_SEC]
            next_window = [time.monotonic() + WINDOW_LOOK_SEC]
            folder = _own_folder(tool)

            def look_elsewhere(elapsed: float, payload: Optional[dict]) -> None:
                on_progress(elapsed, payload)
                if tool.app_id in self._cancelled:
                    return
                now = time.monotonic()
                if now < next_look[0] and now < next_window[0]:
                    return
                pids = desktop.related_pids(folder, {proc.pid})
                if now >= next_look[0]:
                    next_look[0] = now + DISCOVER_EVERY_SEC
                    found = self._discover(tool, pids)
                    if found is not None and health.is_ready(found):
                        discovered["payload"] = found
                        return
                if now >= next_window[0]:
                    next_window[0] = now + WINDOW_LOOK_SEC
                    if pids and desktop.has_window(pids):
                        own_window["pids"] = pids

            payload = health.wait_ready(
                tool.health_url, tool.app_id, timeout=timeout,
                on_progress=look_elsewhere,
                should_stop=lambda: (should_stop() or "payload" in discovered
                                     or "pids" in own_window))
            if payload is None and "payload" in discovered:
                payload = discovered["payload"]
            if payload is None and "pids" in own_window \
                    and tool.app_id not in self._cancelled:
                # 起動確認より先に、ツールが自分の窓を出した。利用者の前には
                # もう画面がある。**それ以上待たせない** (起動中の窓が、
                # ツールの画面の上に居座らない)。起動確認は見回りで続ける
                log.info("ツールが自分の窓を出しました: %s (応答はまだ)",
                         tool.display_name)
                op.step("ツールが自分の窓を出した (起動確認の応答はまだ)")
                how, payload = "window", {}
            if payload is None and tool.app_id not in self._cancelled:
                payload = self._discover(tool, desktop.related_pids(folder, {proc.pid}))
            if payload and int(payload.get("port") or 0) not in (0, tool.port):
                window_note = _port_note(tool, int(payload["port"]))

        if tool.app_id in self._cancelled:
            # 待っているあいだに「やめる」と言われた。**起こしかけた
            # ものを置き去りにしない** ── 誰も知らないまま動き続ける
            log.info("起動をやめました: %s", tool.display_name)
            with self._lock:
                self._processes.pop(tool.app_id, None)
            self._abandon(tool, proc)
            trace.event("起動中止", trace.CANCELLED, tool=tool, op=op,
                        cause="利用者がやめた",
                        elapsed=time.monotonic() - started)
            self._set(self._settled_state(), f"{tool.display_name}の起動をやめました",
                      tool)
            return

        if payload is None:
            if tool.is_app and early_exit.get("code") == 0:
                # すぐ戻り値 0 で終わった。**すでに動いている同じアプリへ
                # 引き渡して終わった** (1つしか起動させないアプリ) のかもしれない
                found = self._find_existing(tool)
                if found is not None:
                    with self._lock:
                        self._processes.pop(tool.app_id, None)
                        self._running[tool.app_id] = found
                        self._failures[tool.app_id] = 0
                    runtime_state.put(found)
                    trace.event("引き継ぎ", trace.INFO, tool=found, op=op,
                                cause="すでに動いていた同じアプリへ引き渡された")
                    self._show(tool, found, op)
                    return
            pids = desktop.related_pids(_own_folder(tool), {proc.pid}) \
                if not tool.is_app else set()
            self._report_start_failure(tool, proc, early_exit,
                                       time.monotonic() - started, op=op,
                                       last_payload=seen.get("payload"),
                                       timeout=timeout)
            if pids and early_exit.get("code") is None \
                    and not desktop.has_window(pids):
                # 応答しないまま動いている。**残すと、次に押したとき2つ目を
                # 起こす** (ポートの取り合い)。起こしたものは片付ける
                # (窓を出しているものは、利用者が見ているので落とさない)
                self._kill_started(tool, proc)
            with self._lock:
                self._processes.pop(tool.app_id, None)
            return

        if how in ("window", "alive") and not tool.is_app:
            # ブラウザーで使うツールが、自分の窓を出した。画面はツールの窓
            # なので**ランチャーはブラウザーを開かない** (開けば画面が2枚)
            running = _running_from_health(tool, {}, launch_pid=proc.pid)
            running.pid = proc.pid
            running.confirmed = False
        elif how in ("window", "alive"):
            # 窓が出た (またはプロセスが動いている)。起動確認はまだでも、
            # 利用者の前にはもう画面がある。**動いているものとして扱う**
            # ── 起動中の窓を出したままにしない・もう一度押しても2つ目を
            # 起こさない。起動確認は見回りのなかで続ける
            running = _running_for_app(tool, proc.pid,
                                       confirmed=not tool.health_url)
        else:
            running = _running_from_health(tool, payload, launch_pid=proc.pid)
            if tool.is_app and int(payload.get("port") or 0) != tool.port \
                    and payload.get("_health_url"):
                window_note = _port_note(tool, int(payload["port"]))
        with self._lock:
            self._running[tool.app_id] = running
            self._failures[tool.app_id] = 0
        # **どの版が動き出したかを残す。** 「入れ替えたのに直らない」を
        # 調べるとき、ログにこの1行があるかどうかで手間が変わる
        log.info("起動完了: %s (版 %s)", running.summary(),
                 payload.get("version") or "不明")
        trace.event("起動完了", trace.OK, tool=running, op=op,
                    version=str(payload.get("version") or ""),
                    elapsed=time.monotonic() - started)

        if window_note and "ポート" in window_note:
            trace.event("ポートちがい", trace.WARNING, tool=running, op=op,
                        cause=f"設定のポート {tool.port} ではなく {running.port} で応答")
        if tool.is_app or how == "window":
            # **ブラウザーは開かない。** アプリ (ツール自身) の窓が画面
            runtime_state.put(running)
            pids = self._tool_pids(running)
            if pids:
                desktop.bring_to_front(pids)
            self._set(State.RUNNING, self.summary(), tool, detail=window_note,
                      elapsed=time.monotonic() - started, responding=True)
            return

        # **ここで初めて画面を開く** (要件定義書 §7.1 / §20)。
        #
        # ただし `--no-browser` が届いていないときは開かない ──
        # ツールが自分でブラウザーを開いているので、ここでも開けば
        # **同じツールの画面が2枚**になり、作業状態を奪い合う
        if tool.suppresses_browser:
            self._set(State.STARTING, f"{tool.display_name}の画面を開いています...",
                      tool, elapsed=time.monotonic() - started,
                      phase=PHASE_OPEN_BROWSER, timeout=timeout)
            self._open_browser(running, running.url or tool.home_url, op)
        else:
            log.info("画面はツール側が開きます: %s", tool.display_name)
        # 画面のPIDまで入った状態で記録する。`stop.bat` は別プロセス
        # なので、書いておかないとそちらから画面を閉じられない
        runtime_state.put(running)

        detail = window_note
        if not tool.suppresses_browser:
            detail = "\n\n".join(x for x in (window_note,
                                               _tool_opens_browser_note(tool)) if x)
        elif not running.browser_managed:
            detail = ("画面は既定のブラウザーで開きました。\n"
                      "止めるときに自動では閉じないので、"
                      "不要になったタブは手で閉じてください。")
        self._set(State.RUNNING, self.summary(), tool, detail=detail,
                  elapsed=time.monotonic() - started, responding=True)

    def _wait_app(self, tool: Tool, proc: subprocess.Popen, timeout: float,
                  early_exit: dict, seen: dict,
                  op: Optional[trace.Operation] = None
                  ) -> tuple[str, Optional[dict], str]:
        """自分の窓を出すアプリの起動を待つ。`(どう起動したか, 応答, 案内)`。

        どう起動したか:

            "health"    起動確認が答えた (別のポートで答えたものも含む)
            "window"    **ツールの窓が出た** (起動確認はまだでもよい)
            "alive"     窓も応答も確かめられないが、ツールのプロセスは動いている
            "failed"    ツールのプロセスが無くなった
            "cancelled" 待っているあいだに「やめる」と言われた

        **起動した exe がすぐ終わる作り**がある (本体を別に起こして戻り値 0 で
        終わる)。起こした exe だけを見ず、ツールのフォルダーから起動した
        プロセスと、その子・孫を見る。窓が出たら、それ以上待たせない
        (起動中の窓が、出てきたツールの画面の上に居座らないように)。
        """
        folder = _own_folder(tool)
        limit = timeout if tool.health_url else min(timeout, APP_WINDOW_WAIT_SEC)
        began = time.monotonic()
        next_look = began + DISCOVER_EVERY_SEC
        exited_at: Optional[float] = None
        while True:
            if tool.app_id in self._cancelled:
                return "cancelled", None, ""
            elapsed = time.monotonic() - began
            code = proc.poll()
            if code is not None:
                early_exit["code"] = code
                exited_at = exited_at or time.monotonic()
            pids = desktop.related_pids(folder, {proc.pid})
            if not pids:
                if code is not None and (
                        code != 0 or time.monotonic() - exited_at >= STUB_GRACE_SEC):
                    return "failed", None, ""
            elif code is not None:
                early_exit.pop("code", None)  # 起動用の exe。本体は動いている

            payload = None
            if tool.health_url:
                payload = health.probe(tool.health_url)
                if payload:
                    if "payload" not in seen and op is not None:
                        op.step(f"初めて応答あり ({elapsed:.1f}秒): "
                                f"app_id={payload.get('app_id', '?')}")
                    seen["payload"] = payload
                if health.is_tool(payload, tool.app_id) and health.is_ready(payload):
                    return "health", payload, ""
                if pids and time.monotonic() >= next_look:
                    next_look = time.monotonic() + DISCOVER_EVERY_SEC
                    found = self._discover(tool, pids)
                    if found is not None and health.is_ready(found):
                        return "health", found, ""

            shown = desktop.has_window(pids) if pids else False
            if not shown and elapsed >= 1.0 and _title_windows(tool):
                shown = True                  # 窓の持ち主はたどれないが、題名で出ている
            stage = health.stage_text(payload) or "窓が出るのを待っています"
            self._set(State.STARTING, f"{tool.display_name}: {stage}", tool,
                      elapsed=elapsed, phase=PHASE_WAIT, stage=stage, timeout=limit)
            if shown:
                log.info("窓が出ました: %s (%.1f秒)", tool.display_name, elapsed)
                return "window", None, ""
            if shown is None and not tool.health_url and pids \
                    and elapsed >= APP_SETTLE_SEC:
                return "window", None, ""     # 窓を見られない環境。生きていればよい
            if elapsed >= limit:
                if pids:
                    log.warning("窓も応答もありませんが、動いています: %s",
                                tool.display_name)
                    return "alive", None, (
                        f"{tool.display_name}は動いていますが、{limit:.0f}秒待っても"
                        "窓も起動確認の応答もありませんでした。\n"
                        "通知領域 (画面右下の ^) に入っていないか見てください。"
                        + (f"\n起動確認: {tool.health_url}" if tool.health_url else ""))
                return "failed", None, ""
            time.sleep(0.25)

    def _kill_started(self, tool: Tool, proc: subprocess.Popen) -> None:
        """起動しかけて応答しないまま動いているものを片付ける (起こしたものだけ)。"""
        tree = desktop.process_tree({proc.pid})
        log.info("起動できなかった %s のプロセスを片付けます: %s",
                 tool.app_id, sorted(tree))
        for pid in sorted(tree, reverse=True):
            process_manager._terminate(pid, force=True)
        try:
            proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    def _spawn(self, tool: Tool,
               op: Optional[trace.Operation] = None) -> Optional[subprocess.Popen]:
        """起動入口を実行する (要件定義書 §12.2)。

        ランチャーはツールの起動方法を知らない。入口を呼ぶだけにして、
        Pythonの起こし方は各リポジトリ側に残す。
        """
        command = _entry_command(tool)
        out_path = tool_log_path(tool.app_id)
        log.info("起動開始: %s — %s (cwd=%s)",
                 tool.display_name, " ".join(command), tool.resolved_work_dir)
        trace.event("起動開始", trace.INFO, tool=tool, op=op,
                    detail=f"{' '.join(command)} (作業フォルダー {tool.resolved_work_dir or '-'})")
        if tool.start_args.strip() and not tool.forwards_args:
            # **届かない引数を、届いたつもりで扱わない。**
            # `.vbs` は引数を転送するとは限らない (4ツールの Start.vbs は
            # 転送しない)。気づかないと「--no-browser を付けたのに
            # タブが開く」の原因が分からなくなる
            log.warning("起動引数は届きません (%s は引数を転送しません): %s",
                        Path(tool.start_command).name, tool.start_args)

        # 大きくなっていれば退ける。**日付で分かれないので、開いたまま
        # 長く使う端末では際限なく育つ**
        logging_utils.rotate_if_large(
            out_path,
            max_bytes=int(app_config.log_setting("tool_log_max_mb")) * 1024 * 1024,
            keep=int(app_config.log_setting("tool_log_keep")))

        try:
            # 出力はファイルへ逃がす。**パイプで受けてはいけない** ──
            # 業務ツールはこのプロセスが生きているあいだ動き続けるので、
            # 誰も読まないパイプはやがて詰まり、ツールごと固まる。
            # ファイルなら詰まらず、しかも調査の材料になる (§16)
            out = open(out_path, "a", encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("出力ログを開けませんでした (%s): %s", out_path, exc)
            out = subprocess.DEVNULL

        try:
            proc = subprocess.Popen(
                command,
                cwd=tool.resolved_work_dir or None,
                # BATの末尾の `pause` で固まらせない。標準入力が
                # 最初から終わっていれば `pause` はすぐ抜ける
                stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT,
                creationflags=NO_WINDOW | NEW_GROUP,
                shell=False)
        except (OSError, subprocess.SubprocessError) as exc:
            log.exception("start.bat を実行できませんでした: %s", tool.start_command)
            cause = f"start.bat を実行できなかった ({type(exc).__name__}: {exc})"
            work_dir = tool.resolved_work_dir
            path = trace.incident(
                f"{tool.display_name}を起動できなかった", tool=tool, op=op,
                whys=["ツールのバックエンドが立ち上がらなかった",
                      cause],
                observed=[("実行したコマンド", " ".join(command)),
                          ("起動ファイル", tool.start_command),
                          ("ファイルがあるか",
                           fileprobe.probe(tool.start_command).describe()),
                          ("作業フォルダー", work_dir or "-"),
                          ("フォルダーがあるか",
                           "-" if not work_dir else
                           ("ある" if os.path.isdir(work_dir) else
                            "無い (または確かめられない)"))],
                hints=["ファイルの場所が変わっていないか (設定画面の［参照］で指定し直す)",
                       "ウイルス対策ソフトなどに実行を止められていないか"],
                exc=exc)
            trace.event("起動失敗", trace.FAILED, tool=tool, op=op, cause=cause,
                        incident=path)
            self._set(State.ERROR, f"{tool.display_name}を起動できませんでした", tool,
                      detail=(f"start.bat を実行できませんでした: {exc}\n"
                              f"パス: {tool.start_command}\n" + _records_note(path)),
                      incident=path)
            return None
        finally:
            if out is not subprocess.DEVNULL:
                out.close()                   # 子プロセス側は自分の複製を持つ

        return proc

    def _abandon(self, tool: Tool, proc: subprocess.Popen) -> None:
        """起こしかけたツールを片付ける。

        起動をやめたとき、こちらは記録を持たないまま去るので、
        止める人が誰も居なくなる。**立ち上がっていれば止める**。
        """
        payload = health.probe(tool.health_url)
        if health.is_tool(payload, tool.app_id):
            running = _running_from_health(tool, payload, launch_pid=proc.pid)
            log.info("打ち切ったツールを止めます: %s", running.summary())
            process_manager.stop(running, force=True, timeout=10)
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=3)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

    def _report_start_failure(self, tool: Tool, proc: subprocess.Popen,
                              early_exit: dict, elapsed: float, *,
                              op: Optional[trace.Operation] = None,
                              last_payload: Optional[dict] = None,
                              timeout: float = 0.0) -> None:
        """起動できなかったことを、**次に何を見ればよいか**まで含めて出す。

        画面には短い案内を、障害記録には**ランチャーが確かめた事実**を
        「なぜ」の形で並べる。どこまで進んで止まったか (入口が落ちた・
        ポートが開かない・別のアプリが答えた・準備が終わらない) で、
        次に調べるところがまったく違うので、ここで分けておく。
        """
        out_path = tool_log_path(tool.app_id)
        tail = trace.tail_lines(out_path)
        error = trace.error_line(tail)
        if tool.is_app:
            self._report_app_failure(tool, early_exit.get("code"), elapsed,
                                     error=error, out_path=out_path, op=op)
            return
        entry = Path(tool.start_command.strip().strip('"')).name or "起動ファイル"
        lines: list[str] = []
        whys = [f"起動確認 ({tool.health_url}) から準備完了の応答が無かった"
                f" (待った時間 {elapsed:.1f}秒)"]
        hints: list[str] = []

        code = early_exit.get("code")
        exit_now = proc.poll() if code is None else code
        port_open = health.is_port_accepting(tool.port)
        payload = health.probe(tool.health_url) if port_open else None
        answer = payload or last_payload
        other = (str(answer.get("app_id") or "(アプリIDなし)")
                 if answer and not health.is_tool(answer, tool.app_id) else "")

        if code is not None:
            exit_text = desktop.describe_exit_code(code)
            cause = f"{entry} が戻り値 {exit_text} で終了した"
            whys.append(f"ツールが立ち上がる前に、起動ファイルが終了した (戻り値 {exit_text})")
            lines.append(f"{entry} が終了しました (戻り値 {exit_text})。")
            if error:
                whys.append(f"ツールの出力にエラーが出ている: {error}")
            if other:
                # 入口が落ちた理由として、確かめられた事実を並べておく
                whys.append(f"ポート {tool.port} では、別のアプリ (アプリID {other})"
                            " が応答している")
                lines.append(f"ポート {tool.port} は別のアプリ ({other}) が"
                             "使っています。")
                hints.append("設定画面で、ほかのツールと同じポートになっていないか")
            if tool.start_args.strip():
                lines.append(f"起動引数「{tool.start_args}」を受け付けない"
                             "ツールかもしれません。設定画面で空にして"
                             "試してください。")
                hints.append(f"起動引数「{tool.start_args}」を受け付けないツールでは"
                             "ないか (設定画面で空にして試す)")
            hints.append(f"{entry} をダブルクリックして、出るエラーを読む")
        elif other:
            cause = f"ポート {tool.port} で別のアプリ ({other}) が応答している"
            whys.append(f"ポート {tool.port} では、別のアプリ (アプリID {other}) が"
                        "応答している")
            lines.append(f"ポート {tool.port} は別のアプリ ({other}) が使っています。")
            hints.append("設定画面で、ほかのツールと同じポートになっていないか")
            hints.append(f"{other} を止めてから、もう一度押してみる")
        elif answer and health.is_tool(answer, tool.app_id):
            stage = health.stage_text(answer) or "不明"
            cause = f"準備が {elapsed:.0f}秒で終わらなかった (段階: {stage})"
            whys.append(f"ツールは応答しているが、準備が終わらなかった (段階: {stage})")
            lines.append(f"{tool.display_name}は応答していますが、"
                         f"準備が終わりませんでした (段階: {stage})。")
            hints.append("データの読み込みに時間がかかっていないか "
                         "(待つ上限は launcher.json の start_timeout_seconds)")
            hints.append("ツールの出力に、準備の途中で止まった跡がないか")
        elif port_open:
            cause = f"ポート {tool.port} は待ち受けているが、起動確認に応答しない"
            whys.append(f"ポート {tool.port} は待ち受けているが、"
                        "起動確認に応答しない")
            lines.append(f"ポート {tool.port} は待ち受けていますが、"
                         "起動確認の応答がありません。")
            proxies = health.proxy_settings()
            if proxies:
                proxy_text = "、".join(f"{k}={v}" for k, v in sorted(proxies.items()))
                lines.append(f"プロキシ設定があります ({proxy_text})。"
                             "127.0.0.1 が除外されているか確認してください。")
                hints.append(f"プロキシ ({proxy_text}) が 127.0.0.1 への接続を"
                             "横取りしていないか")
            hints.append("セキュリティ製品が通信を止めていないか")
            lines.append(f"起動確認URL: {tool.health_url}")
        else:
            cause = f"ポート {tool.port} で待ち受けていない"
            whys.append(f"ポート {tool.port} で待ち受けていない "
                        "(ツールのサーバーが立ち上がっていない)")
            if error:
                whys.append(f"ツールの出力にエラーが出ている: {error}")
            lines.append(f"{elapsed:.0f}秒 待ちましたが応答がありませんでした。")
            lines.append(f"起動確認URL: {tool.health_url}")
            hints.append("start.bat をダブルクリックして、出るエラーを読む")
            hints.append(f"ツール側のポートが設定 ({tool.port}) と同じか")

        if error and code is None and not other:
            lines.append(f"ツールの出力のエラー: {error}")

        observed = [
            ("起動ファイル", tool.start_command),
            ("起動引数", tool.start_args or "(なし)"),
            ("作業フォルダー", tool.resolved_work_dir or "-"),
            ("起動ファイルの状態",
             "動いたまま" if exit_now is None else f"終了 (戻り値 {exit_now})"),
            ("待った時間", f"{elapsed:.1f}秒" + (f" (上限 {timeout:.0f}秒)"
                                                if timeout else "")),
            ("ポート", f"{tool.port} は"
                       + ("待ち受けている" if port_open else "待ち受けていない")),
            ("起動確認の応答", _describe_payload(answer)),
            ("プロキシ", "、".join(f"{k}={v}" for k, v in
                                  sorted(health.proxy_settings().items())) or "なし"),
        ]
        path = trace.incident(f"{tool.display_name}を起動できなかった", tool=tool,
                              op=op, whys=whys, observed=observed, hints=hints,
                              tool_log=out_path)
        trace.event("起動失敗", trace.FAILED, tool=tool, op=op, cause=cause,
                    detail=error, elapsed=elapsed, incident=path)

        lines.append(f"ツールの出力: {out_path}")
        lines.append(_records_note(path))
        log.error("起動失敗: %s — %s", tool.display_name, " / ".join(lines))
        self._set(State.ERROR, f"{tool.display_name}を起動できませんでした", tool,
                  detail="\n".join(lines), elapsed=elapsed, incident=path)

    def _report_app_failure(self, tool: Tool, code: Optional[int],
                            elapsed: float, *, error: str, out_path: Path,
                            op: Optional[trace.Operation] = None) -> None:
        """窓を出すアプリ (exe) が、窓を出す前に終わった。"""
        entry = Path(tool.start_command.strip().strip('"')).name
        exit_text = desktop.describe_exit_code(code)
        whys = ["アプリの窓が出なかった",
                f"窓が出る前に、{entry} が終了した (戻り値 {exit_text}、"
                f"{elapsed:.1f}秒後)"]
        if error:
            whys.append(f"アプリの出力にエラーが出ている: {error}")
        hints = [f"{entry} をダブルクリックして、出るメッセージを読む"]
        value = (code or 0) & 0xFFFFFFFF
        if value == 0xC0000135 or desktop.looks_like_tauri(tool.start_command):
            hints.append("Microsoft Edge WebView2 ランタイムが入っているか "
                         "(Tauri のアプリは WebView2 で画面を出す)")
        if tool.start_args.strip():
            hints.append(f"起動引数「{tool.start_args}」を受け付けないアプリではないか")
        hints.append("ウイルス対策ソフトや AppLocker などに実行を止められていないか")
        cause = f"{entry} が窓を出す前に終了した (戻り値 {exit_text})"
        path = trace.incident(
            f"{tool.display_name}を起動できなかった", tool=tool, op=op,
            whys=whys, hints=hints, tool_log=out_path,
            observed=[("起動ファイル", tool.start_command),
                      ("起動引数", tool.start_args or "(なし)"),
                      ("作業フォルダー", tool.resolved_work_dir or "-"),
                      ("画面", "アプリの窓 (Web サーバーなし)"),
                      ("戻り値", exit_text),
                      ("待った時間", f"{elapsed:.1f}秒")])
        trace.event("起動失敗", trace.FAILED, tool=tool, op=op, cause=cause,
                    detail=error, elapsed=elapsed, incident=path)
        lines = [f"{entry} が窓を出す前に終了しました (戻り値 {exit_text})。"]
        if error:
            lines.append(f"出力のエラー: {error}")
        lines.append(_records_note(path))
        log.error("起動失敗: %s — %s", tool.display_name, " / ".join(lines))
        self._set(State.ERROR, f"{tool.display_name}を起動できませんでした", tool,
                  detail="\n".join(lines), elapsed=elapsed, incident=path)

    def _record_stop_failure(self, running: RunningTool, result,
                             *, force: bool,
                             op: Optional[trace.Operation] = None) -> str:
        """止められなかったときの障害記録。場所を返す。"""
        pid_alive = bool(running.pid and process_manager.is_pid_alive(running.pid))
        still = process_manager.is_running(running)
        whys = [f"終了を確認できなかった ({result.message})"]
        if still:
            whys.append("ツールはまだ起動確認に応答している (止める指示が効いていない)")
        elif pid_alive:
            whys.append(f"起動確認には応答しないが、プロセス (PID {running.pid}) は残っている")
        return trace.incident(
            f"{running.display_name or running.app_id}を終了できなかった",
            tool=running, op=op, whys=whys,
            observed=[("止め方の設定", running.stop_method or "auto"),
                      ("最後に試した方法", result.method or "-"),
                      ("強制終了", "はい" if force else "いいえ"),
                      ("停止ファイル", running.stop_command or "-"),
                      ("PID", f"{running.pid or '-'} ("
                              + ("残っている" if pid_alive else "残っていない") + ")"),
                      ("起動確認", "応答あり" if still else "応答なし"),
                      ("ポート", f"{running.port}")],
            hints=["タスクマネージャーで、PID のプロセスが固まっていないか",
                   "ツールの stop.bat を手で実行して、出るエラーを読む"],
            tool_log=tool_log_path(running.app_id))

    # --------------------------------------------------------------
    # 生存監視 (基盤仕様書 2.9)
    # --------------------------------------------------------------
    def poll_health(self) -> bool:
        """動いているツールがまだ応答しているか。画面から定期的に呼ぶ。

        **ブラウザーを閉じたこととツールが落ちたことは別物** なので、
        ここで見るのはツール側。落ちていたら一覧から外し、利用者が
        もう一度押せば起動し直せるようにする (要件定義書 §11)。

        何か1つでも動いていれば真。
        """
        with self._lock:
            targets = [(a, r) for a, r in self._running.items()
                       if a not in self._starting and a not in self._stopping]
        if not targets:
            return bool(self._running)

        # 利用者が手で閉じた画面を片付ける
        browser.reap()
        limit = max(1, int(app_config.ui_setting("health_failures_before_dead")))
        answers = _probe_running([r for _, r in targets])

        for app_id, running in targets:
            if not running.confirmed:
                self._watch_unconfirmed(running, bool(answers.get(app_id)))
                continue
            if running.is_app:
                alive = self._app_alive(running)
                if alive is False:
                    # 窓を持つプロセスが消えたのは**確か**なので、1回で判断する
                    # (HTTP の応答と違い、一時的に途切れることがない)
                    self._app_ended(running)
                    continue
                if alive and not running.health_url:
                    self._failures[app_id] = 0
                    continue
            if answers.get(app_id):
                self._failures[app_id] = 0
                self._unhealthy.discard(app_id)
                self._watch_browser(running)
                continue

            # --- 応答が無い ---
            count = self._failures.get(app_id, 0) + 1
            self._failures[app_id] = count
            if count < limit:
                log.info("応答がありません (%d/%d): %s", count, limit, app_id)
                continue                      # まだ判断しない
            self._lost(running, count)
        return bool(self._running)

    def _watch_unconfirmed(self, running: RunningTool, answered: bool) -> None:
        """起動確認がまだ取れていないツール (窓が出た・動いているので起動
        済みとみなしたもの) を見る。

        **応答が無いことを「落ちた」とは判断しない** ── まだ準備中か、
        設定のポートが違うだけかもしれない。生死はプロセスで見る。起動の
        上限を過ぎても答えなければ、本当のポートを探し、見つからなければ
        1回だけ知らせる。
        """
        app_id = running.app_id
        name = running.display_name or app_id
        if answered:
            running.confirmed = True
            runtime_state.put(running)
            log.info("起動を確かめました: %s", running.summary())
            trace.event("起動を確かめた", trace.OK, tool=running)
            self._set(State.RUNNING, self.summary(), running, responding=True)
            return
        if not self._tool_alive(running):
            if running.is_app:
                self._app_ended(running)
                return
            self._forget(app_id)
            trace.event("自動終了", trace.INFO, tool=running,
                        cause="起動を確かめられないまま終わった")
            self._set(self._settled_state(), f"{name}は終了しました", running)
            return
        limit = float(app_config.ui_setting("start_timeout_seconds"))
        if not running.health_url or app_id in self._unhealthy \
                or time.time() - running.started_at < limit:
            return
        self._unhealthy.add(app_id)
        tool = tool_registry.get(app_id)
        found = self._discover(tool, self._tool_pids(running)) if tool else None
        if found is not None:
            port = int(found["port"])
            running.port = port
            running.health_url = found.get("_health_url") or running.health_url
            running.url = f"http://127.0.0.1:{port}/"
            running.confirmed = True
            runtime_state.put(running)
            trace.event("ポートちがい", trace.WARNING, tool=running,
                        cause=f"設定のポート {tool.port} ではなく {port} で応答")
            self._set(State.RUNNING, self.summary(), running, responding=True,
                      detail=_port_note(tool, port))
            return
        ports = desktop.listening_ports(self._tool_pids(running))
        listening = "、".join(str(p) for _, p in ports) or "なし"
        path = trace.incident(
            f"{name}の起動を確かめられない", tool=running,
            whys=[f"起動確認 ({running.health_url}) に、{limit:.0f}秒たっても応答が無い",
                  f"ツールのプロセスは動いている (待ち受けているポート: {listening})"],
            hints=["設定のポートが、ツールの使うポートと同じか (［設定］のポート)",
                   "ツールに起動確認 (/api/health) があるか。無ければポートを空にする"
                   " (窓で起動を確かめる)"],
            observed=[("起動確認", running.health_url),
                      ("待ち受けているポート", listening)],
            tool_log=tool_log_path(app_id))
        trace.event("起動を確かめられない", trace.WARNING, tool=running,
                    cause=f"待ち受けているポート: {listening}", incident=path)
        if running.is_app:
            # 以後はプロセスと窓で見る (応答が無いたびに知らせない)
            running.health_url = ""
            runtime_state.put(running)
        self._set(State.RUNNING, self.summary(), running, responding=True,
                  incident=path,
                  detail=(f"{name}は動いていますが、起動確認 (ポート {running.port or '?'})"
                          f" に応答しません。\n待ち受けているポート: {listening}\n"
                          "［設定］のポートを確かめてください。\n" + _records_note(path)))

    def _watch_browser(self, running: RunningTool) -> None:
        """画面を閉じたら知らせる。**閉じるとツールはまもなく自分で終わる。**

        各ツールは「誰も見ていなければ終了する」見張りを持っていて、
        画面の心拍が途切れると数秒で落ちる。「画面だけ閉じた状態が
        続く」かのように見せない。
        """
        if not running.browser_managed:
            return
        open_now = process_manager.is_browser_open(running)
        before = self._browser_seen.get(running.app_id)
        self._browser_seen[running.app_id] = open_now
        if before is None or before == open_now or open_now:
            return
        name = running.display_name or running.app_id
        log.info("画面が閉じられました。ツールはまもなく終了します: %s",
                 running.app_id)
        trace.event("画面を閉じた", trace.INFO, tool=running,
                    cause="利用者が画面を閉じた (ツールはまもなく自分で終わる)")
        self._set(State.RUNNING, f"{name}の画面を閉じました（まもなく終了します）",
                  running, responding=True, browser_open=False,
                  detail=(f"{name}のブラウザー画面を閉じました。\n"
                          "ツール側は「誰も見ていない」と判断して、まもなく自動で"
                          "終了します(実行中の処理があれば終わるまで待ちます)。\n\n"
                          "続けて使うときは、もう一度ボタンを押してください。"))

    def _app_alive(self, running: RunningTool) -> Optional[bool]:
        """アプリの窓を持つプロセスが動いているか。

        **ランチャーが起動したものは、起こしたときの手がかり (プロセスの
        ハンドル) で答える。** 道の書き方の違いや PID の使い回しに
        左右されない。ランチャーの外で起動されたものは、exe の道で確かめる。
        """
        if not running.is_app:
            return None
        with self._lock:
            proc = self._processes.get(running.app_id)
        if proc is not None and proc.poll() is None:
            return True
        # 起こした exe が終わっていても、本体が動いていれば動いている
        return bool(self._tool_pids(running))

    def _app_ended(self, running: RunningTool) -> None:
        """アプリの窓が閉じられた (プロセスが終わった)。

        窓を閉じればアプリは終わる。**ふつうのこと**なので、知らせるだけ。
        ただしランチャーが起動したアプリで、戻り値が 0 以外なら異常終了
        (落ちた) ── 障害記録を書く。ランチャーの外で起動されたアプリは
        戻り値が取れないので、閉じたものとして扱う。
        """
        name = running.display_name or running.app_id
        with self._lock:
            proc = self._processes.get(running.app_id)
        code = proc.poll() if proc is not None else None
        self._forget(running.app_id)
        if code is None or code == 0:
            log.info("アプリが終了しました: %s (戻り値 %s)", running.app_id,
                     "不明" if code is None else code)
            trace.event("アプリを閉じた", trace.INFO, tool=running,
                        cause=("アプリの窓が閉じられた" if code == 0 else
                               "アプリが終了した (戻り値は不明)"))
            self._set(self._settled_state(), f"{name}を閉じました", running,
                      detail="")
            return

        exit_text = desktop.describe_exit_code(code)
        error = trace.error_line(trace.tail_lines(tool_log_path(running.app_id)))
        whys = [f"アプリのプロセスが終了した (戻り値 {exit_text})",
                "窓を閉じた終わり方 (戻り値 0) ではない"]
        if error:
            whys.append(f"アプリの出力にエラーが出ている: {error}")
        started = time.strftime("%Y/%m/%d %H:%M:%S",
                                time.localtime(running.started_at))
        path = trace.incident(
            f"{name}が異常終了した", tool=running, whys=whys,
            hints=["アプリの出力の最後に、エラーや panic の跡が無いか",
                   "Windows のイベント ビューアー (Windows ログ → アプリケーション)"
                   " に同じ時刻のエラーが無いか",
                   "同じ操作をすると毎回落ちるか (落ちる直前に何をしていたか)"],
            observed=[("起動した時刻", started),
                      ("起動ファイル", running.start_command),
                      ("PID", str(running.window_pid)),
                      ("戻り値", exit_text)],
            tool_log=tool_log_path(running.app_id))
        trace.event("思わぬ停止", trace.FAILED, tool=running,
                    cause=f"アプリが異常終了した (戻り値 {exit_text})",
                    detail=error, incident=path)
        self._set(State.ERROR, f"{name}が異常終了しました", running,
                  detail=(f"{name}が終了しました (戻り値 {exit_text})。\n"
                          "もう一度ボタンを押すと起動し直します。\n"
                          + _records_note(path)),
                  incident=path)

    def _lost(self, running: RunningTool, count: int) -> None:
        """応答が途切れたツールを一覧から外す。"""
        name = running.display_name or running.app_id
        if running.is_app and self._app_alive(running):
            # 窓は出ているのに、中の Web サーバーが答えない。**アプリは
            # 外さない** (窓は利用者の前にある)。知らせと記録は1回だけ
            self._failures[running.app_id] = 0
            if running.app_id in self._unhealthy:
                return
            self._unhealthy.add(running.app_id)
            path = self._record_lost(running, count)
            self._set(State.ERROR, f"{name}が応答しません", running,
                      detail=(f"{name}の窓は出ていますが、中のサーバー部分が"
                              "応答しません。\nアプリを閉じて、もう一度"
                              "ボタンを押してください。\n" + _records_note(path)),
                      incident=path)
            return
        log.warning("応答が途切れました: %s (%d回連続)", running.summary(), count)
        was_open = (running.browser_managed
                    and process_manager.is_browser_open(running))
        if was_open:
            # 画面は出ているのにツールが答えない。**思わぬ停止**。
            # 一覧から外す前に手がかりを集める (外すとPIDを引き取って
            # しまい、残っていたかどうか分からなくなる)
            path = self._record_lost(running, count)
            self._forget(running.app_id)
            self._set(State.ERROR, f"{name}が終了しました", running,
                      detail=("ツール側が停止したか、応答しなくなりました。\n"
                              "もう一度ボタンを押すと起動し直します。\n"
                              + _records_note(path)),
                      incident=path)
            return
        self._forget(running.app_id)
        trace.event("自動終了", trace.INFO, tool=running,
                    cause="画面が閉じていて、ツールが自分で終わった"
                    if running.browser_managed else
                    "ツールが応答しなくなった (画面はツール側の管理)",
                    detail=f"応答なし {count}回")
        # 画面を閉じたので、ツールが自分で終わった。ふつうのこと
        self._set(self._settled_state(), f"{name}は終了しました", running,
                  detail=("画面を閉じると、ツールは自分で終了します。\n"
                          "続けて使うときは、もう一度ボタンを押してください。"
                          + ("" if running.browser_managed else
                             "\n画面 (タブ) が残っていれば、手で閉じてください。")))

    def _record_lost(self, running: RunningTool, count: int) -> str:
        """思わぬ停止の障害記録。「落ちた」か「固まった」かを分けておく。"""
        interval = app_config.ui_setting("health_poll_seconds")
        pid_alive = bool(running.pid and process_manager.is_pid_alive(running.pid))
        port_open = health.is_port_accepting(running.port)
        whys = [f"起動確認 ({running.health_url}) に {count}回続けて応答しなかった"]
        if pid_alive and port_open:
            whys.append(f"プロセス (PID {running.pid}) もポート {running.port} も"
                        "残っているのに応答しない (固まっている)")
            hints = ["重い処理の途中で固まっていないか (ツールの出力の最後を見る)",
                     "タスクマネージャーで CPU・メモリを使い切っていないか"]
        elif pid_alive:
            whys.append(f"プロセス (PID {running.pid}) は残っているが、"
                        f"ポート {running.port} を閉じている")
            hints = ["終了の途中で止まっていないか (ツールの出力の最後を見る)"]
        else:
            whys.append(f"ツールのプロセス (PID {running.pid or '?'}) が無くなっている"
                        " (落ちた・誰かが止めた)")
            hints = ["ツールの出力の最後に、エラーや Traceback が無いか",
                     "ほかの人・ほかの手段 (stop.bat、タスクマネージャー) で"
                     "止めていないか",
                     "パソコンがスリープから戻った直後ではないか"]
        error = trace.error_line(trace.tail_lines(tool_log_path(running.app_id)))
        if error:
            whys.append(f"ツールの出力にエラーが出ている: {error}")
        started = time.strftime("%Y/%m/%d %H:%M:%S",
                                time.localtime(running.started_at)) \
            if running.started_at else "-"
        path = trace.incident(
            f"{running.display_name or running.app_id}が思わず止まった",
            tool=running, whys=whys, hints=hints,
            observed=[("起動した時刻", started),
                      ("PID", f"{running.pid or '-'} ("
                              + ("残っている" if pid_alive else "残っていない") + ")"),
                      ("ポート", f"{running.port} は"
                                 + ("待ち受けている" if port_open else "待ち受けていない")),
                      ("画面", "出たまま"),
                      ("見回りの間隔", f"{interval}秒 × {count}回")],
            tool_log=tool_log_path(running.app_id))
        trace.event("思わぬ停止", trace.FAILED, tool=running,
                    cause=whys[1] if len(whys) > 1 else whys[0],
                    detail=error, incident=path)
        return path

    # --------------------------------------------------------------
    # 後始末
    # --------------------------------------------------------------
    def shutdown(self, *, stop_tools: bool, force: bool = False) -> bool:
        """ランチャーを終わるとき。止められたかを返す。

        業務ツールまで止めるかは利用者に選ばせる ── ブラウザーを閉じる
        こととバックエンドを止めることは別 (要件定義書 §11)。長い処理の
        途中でランチャーを閉じただけで落とされるのは困る。

        止めずに終わったツールは動き続け、次にランチャーを起動したとき
        引き継ぐ。画面を閉じれば、各ツールは自分で終わる。

        **止まらなかったことを黙って飲み込まない。** 実行中の処理が
        あって止められなかったのに閉じてしまうと、利用者は「止めた
        つもり」で残ったツールに気づけない。
        """
        if stop_tools:
            return self._stop_all_blocking(force=force)
        # 「動かしたまま」。**起動の最中のものもやめさせない** ── 以前は
        # ここで起動をやめさせていて、「いいえ」を選んでも起動中のツールは
        # 止まっていた。そのまま立ち上がれば、次のランチャーが引き継ぐ
        with self._lock:
            pending = list(self._processes)
            running = dict(self._running)
            starting = set(self._starting)
        for app_id in pending:
            if app_id in starting or _launched_exe(running.get(app_id)):
                # **exe はツールそのもの、起動中のものは起動の途中。**
                # 引き取ろうとして落とすと、動かしたままにしたはずのツールが
                # 消える。手放すだけにする
                with self._lock:
                    proc = self._processes.pop(app_id, None)
                if proc is not None:
                    _DETACHED.append(proc)
                continue
            self._reap_process(app_id)
        return True

    # --------------------------------------------------------------
    def _open_browser(self, running: RunningTool, url: str,
                      op: Optional[trace.Operation] = None) -> None:
        """画面を開き、閉じるための手がかりを記録に残す。

        専用プロファイルのアプリウィンドウとして開けたときだけ、PIDと
        プロファイルの道が入る。既定ブラウザーへ渡しただけのときは
        空のままで、止めるときに閉じられないことが記録に残る。
        """
        if not url:
            return
        try:
            session = browser.open_window(running.app_id, url)
        except Exception as exc:              # noqa: BLE001 - 開けなくても続ける
            log.warning("画面を開けませんでした (%s): %s", url, exc)
            trace.event("画面を開けない", trace.WARNING, tool=running, op=op,
                        cause=f"{type(exc).__name__}: {exc}", detail=url)
            return
        running.browser_pid = session.pid
        running.browser_profile = session.profile_dir
        with self._lock:
            known = self._running.get(running.app_id) is running
        if known:
            runtime_state.put(running)
            self._browser_seen[running.app_id] = True


# 起動時に動いているツールを探すとき、同時に当たる数の上限
PROBE_WORKERS = 8


def _probe_all(tools: list[Tool]) -> dict[str, Optional[dict]]:
    """全ツールの `/api/health` を**同時に**当たる。アプリIDごとの応答。

    1つずつ当たると、ツールの数だけ待ちが積み重なる。Windows では
    閉じているポートへの接続が断られるまで1〜2秒かかることがあり、
    5ツールなら**ランチャーの起動だけで数秒**になる。同時に当たれば、
    いちばん遅い1つぶんで済む。
    """
    targets = [t for t in tools if t.health_url]
    if not targets:
        return {}
    with ThreadPoolExecutor(max_workers=min(PROBE_WORKERS, len(targets)),
                            thread_name_prefix="probe") as pool:
        answers = pool.map(lambda t: health.probe(t.health_url), targets)
        return {t.app_id: payload for t, payload in zip(targets, answers)}


def _probe_running(records: list[RunningTool]) -> dict[str, bool]:
    """動いているはずのツールを**同時に**当たる。答えたかどうか。"""
    if not records:
        return {}
    with ThreadPoolExecutor(max_workers=min(PROBE_WORKERS, len(records)),
                            thread_name_prefix="alive") as pool:
        answers = pool.map(process_manager.is_running, records)
        return {r.app_id: bool(alive) for r, alive in zip(records, answers)}


def _running_from_health(tool: Tool, payload: Optional[dict],
                         *, launch_pid: int) -> RunningTool:
    """`/api/health` の応答から、止めるときに要る情報を組み立てる。

    **PIDとポートは応答から取る。** ランチャーが起こしたのは `cmd.exe`
    であって業務ツール本体ではないので、そちらのPIDでは止められない
    (要件定義書 §10)。
    """
    payload = payload or {}
    port = int(payload.get("port") or tool.port or 0)
    url = f"http://127.0.0.1:{port}/" if port else tool.home_url
    health_url = tool.health_url
    if payload.get("_health_url"):
        # 設定とは別のポートで答えた。**答えた場所**を記録する
        health_url = payload["_health_url"]
        base = health_url.split("/", 3)
        url = "/".join(base[:3]) + "/"
    return RunningTool(
        app_id=tool.app_id,
        display_name=tool.display_name,
        pid=int(payload.get("pid") or 0),
        launch_pid=launch_pid,
        port=port,
        url=url,
        health_url=health_url,
        app_root=str(payload.get("app_root") or tool.resolved_work_dir),
        start_command=tool.start_command,
        work_dir=tool.resolved_work_dir,
        stop_command=tool.stop_command,
        stop_method=tool.stop_method,
        ui_mode=tool.resolved_ui_mode,
    )


def _running_for_app(tool: Tool, pid: int, *,
                     confirmed: Optional[bool] = None) -> RunningTool:
    """窓 (またはプロセス) で起動を確かめたアプリの記録。PIDは exe そのもの。

    起動確認 (/api/health) があるのにまだ答えていなければ `confirmed` は偽。
    """
    return RunningTool(
        app_id=tool.app_id,
        display_name=tool.display_name,
        pid=pid,
        launch_pid=pid,
        port=tool.port,
        url=tool.home_url,
        health_url=tool.health_url,
        app_root=tool.resolved_work_dir,
        start_command=tool.start_command,
        work_dir=tool.resolved_work_dir,
        stop_command=tool.stop_command,
        stop_method=tool.stop_method,
        ui_mode=tool_registry.UI_APP,
        confirmed=(not tool.health_url) if confirmed is None else confirmed,
    )


def _find_app_pid(tool: Tool) -> int:
    """そのアプリのプロセス。exe そのものが動いていればそれ、無ければ
    ツールのフォルダーから起動したもの。無ければ 0。"""
    exe = tool.start_command.strip().strip('"')
    if not exe.lower().endswith(".exe"):
        return 0
    pids = desktop.find_by_exe(exe)
    if pids:
        return pids[0]
    folder = _own_folder(tool)
    pids = desktop.processes_in_folder(folder) if folder else []
    return min(pids) if pids else 0


def _find_running_app(tool: Tool) -> Optional[RunningTool]:
    """ランチャーの外で動いている、そのアプリ。無ければ None。"""
    pid = _find_app_pid(tool)
    return _running_for_app(tool, pid) if pid else None


def _own_folder(item) -> str:
    """そのツールのフォルダー。**ほかのツールと共有していれば空**
    (フォルダーでツールを見分けられない)。`Tool` でも `RunningTool` でもよい。"""
    if isinstance(item, RunningTool):
        folder = process_manager.app_folder(item)
    else:
        folder = item.resolved_work_dir
    if not folder:
        return ""
    try:
        if tool_registry.shared_folder(folder, item.app_id):
            return ""
    except Exception:                         # noqa: BLE001 - 分からなければ使わない
        return ""
    return folder


def _title_windows(tool: Tool) -> list[int]:
    """題名がツールの表示名そのもの (か「表示名 - …」) の窓。

    ツールの画面がふだんのブラウザーの窓だと、窓の持ち主はブラウザー本体で
    ツールのプロセスからたどれない。**前に出す・出たかを見るだけ**に使う。
    """
    name = (tool.display_name or "").strip()
    found = desktop.windows_by_title(name) if len(name) >= 2 else None
    return [hwnd for hwnd, title in (found or [])
            if title.strip() == name or title.startswith(name + " - ")
            or title.startswith(name + " ー ")]


def _front_by_title(tool: Tool) -> bool:
    windows = _title_windows(tool)
    return bool(windows) and desktop.activate(windows[0])


def _port_note(tool: Tool, port: int) -> str:
    """設定と違うポートで動いていたときの案内。"""
    return (f"{tool.display_name}は、設定のポート {tool.port or '(空)'} ではなく "
            f"{port} で動いています。\n［設定］でポートを {port} にすると、"
            "起動の確認が早くなります。")


def _launched_exe(record: Optional[RunningTool]) -> bool:
    """exe を直接起動したツールか (受け皿の cmd.exe が無い)。"""
    if record is None:
        return False
    return record.start_command.strip().strip('"').lower().endswith(".exe")


def _entry_command(tool: Tool) -> list[str]:
    """起動入口を実行するためのコマンド。

    `.vbs` は `wscript.exe` 経由で呼ぶ。`Popen` が使う `CreateProcess` は
    **ファイルの関連付けを解決しない**ので、`.vbs` を直接渡しても動かない
    (エクスプローラのダブルクリックとは仕組みが違う)。`.exe` はそのまま。
    """
    path = tool.start_command.strip().strip('"')
    args = tool.start_args.split()
    if tool.entry_kind == "vbs":
        return ["wscript.exe", path] + args
    # .bat と .exe はそのまま実行する。exe には引数がそのまま届く
    return [path] + args


def _leftover_note(running: RunningTool, name: str) -> str:
    """止めたあとに残る画面の案内。残らなければ空。

    * ランチャーが開いた専用画面 → ふつうは閉じてある。閉じきれずに
      残っていれば、そう伝える
    * ツールが自分で開いた画面・既定のブラウザーへ渡した画面・ランチャーの
      外で起動されたツールの画面 → **閉じる手がかりが無い**ので残る
    """
    if running.is_app:
        return ""                             # 画面はアプリ自身の窓。一緒に閉じた
    if running.browser_managed:
        if running.browser_pid and process_manager.is_browser_open(running):
            return (f"{name}の画面を閉じられませんでした。\n"
                    "ブラウザーの画面を手で閉じてください。")
        return ""
    return (f"{name}の画面（ふだんのブラウザーのタブ）は、"
            "ランチャーからは閉じられません。\n"
            "ツールは終了しているので、タブを手で閉じてください。")


def _tool_opens_browser_note(tool: Tool) -> str:
    """ツール側が画面を開く場合の案内。"""
    lines = [f"{tool.display_name}の画面はツール側が開きます"
             "（ふだんのブラウザーに出ます）。"]
    if tool.start_args.strip() and not tool.forwards_args:
        lines.append(f"起動引数「{tool.start_args}」は "
                     f"{Path(tool.start_command).name} が転送しないため"
                     "届いていません。")
    lines.append("止めるときランチャーからは閉じられないので、"
                 "不要になった画面は手で閉じてください。"
                 "(画面を閉じると、ツールは自分で終了します)")
    return "\n".join(lines)


def tool_log_path(app_id: str) -> Path:
    """ツールの出力 (`start.bat` の標準出力) の置き場所。

    **いつも端末の中。** ツールが書き続けるファイルなので、共有フォルダー
    に置くとネットワークが切れたときにツールの書き込みが失敗しかねない。
    障害記録には末尾を写す (`trace`)。
    """
    return app_config.local_dir("logs") / f"tool_{_safe_name(app_id)}.out.log"


def _records_note(incident: str) -> str:
    """案内の末尾に付ける「記録の場所」。

    **ほかのアプリから見た場所** (`trace.real_path`) を書く。Microsoft Store
    版の Python では、ランチャーに見える場所をエクスプローラで探しても
    見つからない。中身は［詳細］でランチャーが読んで見せる。
    """
    if incident:
        return f"障害記録: {trace.real_path(incident)}"
    return f"ランチャーのログ: {trace.real_path(trace.destination().path)}"


def _describe_payload(payload: Optional[dict]) -> str:
    """起動確認の応答を1行で。"""
    if not payload:
        return "無し"
    keys = ("app_id", "ready", "stage", "version", "pid", "port")
    parts = [f"{k}={payload[k]}" for k in keys if k in payload]
    return ", ".join(parts) or "(中身なし)"


def _safe_name(app_id: str) -> str:
    """アプリIDをファイル名に使える形へ。"""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in app_id)
