#!/usr/bin/env python3
"""ツールの起動・切替・停止 (要件定義書 §7 / §8 / §9)

ランチャーの中身はほぼこれ1つ。**業務ロジックは持たない** ──
各ツールの起動方法は、それぞれのリポジトリの `start.bat` に集約する
(要件定義書 §12.2 / §21)。ここが持つのは順序と状態だけ:

    ボタンが押された
       ↓
    同じツールが動いている?  → はい: 画面を出すだけ (二重起動しない §9)
       ↓ いいえ
    別のツールが動いている?  → はい:
           そのツールの画面を閉じる        (§8.3)
           そのツールのバックエンドを止める (§8.1)
           終了を確認する
       ↓
    start.bat を実行する
       ↓
    /api/health が応答するまで待つ  ← **ここを飛ばさない** (§7.2)
       ↓
    ブラウザー画面を開く
       ↓
    現在：<ツール名>

画面とバックエンドは別々に扱う (§11)。利用者が画面だけ手で閉じても
バックエンドは動いたままで、ランチャーはその状態を把握する。

画面 (tkinter) はこのモジュールを読み込むが、**このモジュールは画面を
読み込まない**。起動・停止の判断だけを持つので、画面が無い環境でも
試験できる (基盤仕様書 2.5「起動制御と業務ロジックを分ける」)。
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import process_manager  # noqa: E402
from launcher import (app_config, browser, health, runtime_state,  # noqa: E402
                      tool_registry)
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

    IDLE = "idle"                # 何も起動していない
    STARTING = "starting"        # 起動しています...
    RUNNING = "running"          # 現在：<ツール名>
    STOPPING = "stopping"        # 終了しています...
    ERROR = "error"              # 起動できませんでした


@dataclass
class Status:
    """画面に渡す1枚。**画面はこれだけを見て描く**。"""

    state: State = State.IDLE
    app_id: str = ""
    display_name: str = ""
    message: str = ""
    # 利用者が次に何をすればよいか。エラーのときだけ入る
    detail: str = ""
    elapsed: float = 0.0
    # バックエンドが応答しているか (基盤仕様書 2.9)。
    # ブラウザーを閉じたことと、ツールが落ちたことは別物
    responding: bool = False
    # ランチャーが開いた画面がまだ出ているか (要件定義書 §8.3 / §11)。
    # `responding` とは**別に持つ** ── 画面だけ閉じられた状態を
    # 「ツールが落ちた」と取り違えないため
    browser_open: bool = False
    # その画面をランチャーが閉じられるか。既定ブラウザーへ渡しただけの
    # ときは False で、切り替えのとき手で閉じてもらうことになる
    browser_managed: bool = False

    @property
    def busy(self) -> bool:
        return self.state in (State.STARTING, State.STOPPING)


StatusCallback = Callable[[Status], None]


class ToolManager:
    """起動中のツールを1つだけ持つ。

    同時に1つしか動かさないのは、要件定義書 §8 が「AからBへ切り替える」
    ときにAを止めると決めているため。将来2つ並べたくなったら、ここを
    辞書にすれば済むように、状態はすべてこのクラスの中に閉じてある。
    """

    def __init__(self, on_status: Optional[StatusCallback] = None) -> None:
        self._on_status = on_status
        self._status = Status()
        # 起動・停止は時間がかかるので別スレッドで行う。画面を固めない
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        # 要求ごとに増える番号。**古い待機を打ち切るための印**。
        # 日報の起動を待っている最中に看板を押されたら、日報の待機は
        # そこでやめる (でないと2つの待機が同時に画面を書き換える)
        self._generation = 0
        self._current: Optional[RunningTool] = runtime_state.read()
        self._process: Optional[subprocess.Popen] = None

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
    def current(self) -> Optional[RunningTool]:
        return self._current

    def _emit(self, status: Status) -> None:
        self._status = status
        if self._on_status is not None:
            try:
                self._on_status(status)
            except Exception:                 # noqa: BLE001 - 画面の都合で処理を止めない
                log.exception("状態の通知に失敗しました")

    def _set(self, state: State, message: str, tool: Optional[Tool] = None,
             *, detail: str = "", elapsed: float = 0.0,
             responding: bool = False,
             browser_open: Optional[bool] = None) -> None:
        if browser_open is None:
            browser_open = (self._current is not None
                            and process_manager.is_browser_open(self._current))
        managed = self._current is not None and self._current.browser_managed
        self._emit(Status(
            state=state,
            app_id=tool.app_id if tool else (self._current.app_id
                                             if self._current else ""),
            display_name=(tool.display_name if tool
                          else (self._current.display_name
                                if self._current else "")),
            message=message, detail=detail, elapsed=elapsed,
            responding=responding, browser_open=browser_open,
            browser_managed=managed))

    # --------------------------------------------------------------
    # 起動していたものを引き継ぐ
    # --------------------------------------------------------------
    def adopt_running(self) -> Optional[RunningTool]:
        """すでに動いているツールを見つけて、現在のツールとして扱う。

        2つの場面がある。ランチャーを再起動したとき (記録は残っている)
        と、利用者が `Start.vbs` から直接ツールを起動していたとき。
        どちらも「動いているのに、ランチャーは何も知らない」状態で、
        そのままボタンを押すと二重起動になる (要件定義書 §9)。
        """
        recorded = runtime_state.read()
        if recorded is not None and process_manager.is_running(recorded):
            self._current = recorded
            log.info("動いているツールを引き継ぎました: %s", recorded.summary())
            self._set(State.RUNNING, f"現在：{recorded.display_name}",
                      responding=True)
            return recorded

        # 記録が無い、または応答しない。設定されているツールを順に当たる
        for tool in tool_registry.all_tools():
            payload = health.probe(tool.health_url)
            if not health.is_tool(payload, tool.app_id):
                continue
            running = _running_from_health(tool, payload, launch_pid=0)
            self._current = running
            runtime_state.write(running)
            log.info("ランチャー外で動いているツールを見つけました: %s",
                     running.summary())
            self._set(State.RUNNING, f"現在：{tool.display_name}", responding=True)
            return running

        if recorded is not None:
            # 応答しない記録が残っている。片付けておく
            runtime_state.clear()
        self._current = None
        self._set(State.IDLE, "起動していません")
        return None

    # --------------------------------------------------------------
    # ボタンが押された
    # --------------------------------------------------------------
    def select(self, app_id: str) -> None:
        """ツールのボタンが押されたときの入口。すぐ戻る (処理は別スレッド)。"""
        tool = tool_registry.get(app_id)
        if tool is None:
            self._set(State.ERROR, "登録されていないツールです",
                      detail=f"アプリID: {app_id}")
            return

        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                # 前の要求がまだ動いている。番号を進めれば、そちらは
                # 次の節目で自分から抜ける。連続して押されても、
                # 最後に押したものが残る
                log.info("前の処理を打ち切ります: %s", tool.display_name)
            self._generation += 1
            generation = self._generation
            self._worker = threading.Thread(
                target=self._run_select, args=(tool, generation),
                name=f"select-{tool.app_id}", daemon=True)
            self._worker.start()

    def _run_select(self, tool: Tool, generation: int) -> None:
        try:
            self._select_blocking(tool, generation)
        except Exception as exc:                  # noqa: BLE001 - 画面に出して継続
            log.exception("起動処理で予期しない失敗: %s", tool.app_id)
            self._set(State.ERROR, f"{tool.display_name}を起動できませんでした",
                      tool, detail=f"{exc}\nログ: {app_config.local_dir('logs')}")

    def _select_blocking(self, tool: Tool, generation: int) -> None:
        """起動・切替の本体。試験からはこちらを直接呼ぶ。"""
        # --- すでに同じツールが動いている (要件定義書 §9) ---
        current = self._current
        if current is not None and current.app_id == tool.app_id:
            if process_manager.is_running(current):
                if process_manager.is_browser_open(current):
                    # 画面はもう出ている。**もう1枚開かない** ──
                    # 同じツールの窓が2つ並ぶほうが分かりにくい
                    log.info("すでに動いていて画面も出ています: %s",
                             tool.display_name)
                else:
                    # 利用者が画面だけ手で閉じていた。バックエンドは
                    # 動いたままなので、起動し直さず画面だけ開く (§11)
                    log.info("画面だけ開き直します: %s", tool.display_name)
                    self._open_browser(current, current.url or tool.home_url)
                self._set(State.RUNNING, f"現在：{tool.display_name}", tool,
                          responding=True)
                return
            # 記録はあるが応答しない。落ちている。起動し直す
            log.info("%s は応答しないので起動し直します", tool.display_name)
            self._current = None
            runtime_state.clear()

        # --- 別のツールが動いている (要件定義書 §8) ---
        if self._current is not None:
            if not self._stop_current(generation):
                return                        # 止められなかった。理由は出してある
            if self._superseded(generation):
                return

        self._start(tool, generation)

    # --------------------------------------------------------------
    # 停止
    # --------------------------------------------------------------
    def stop_current(self, *, force: bool = False) -> None:
        """「終了」が押されたとき。すぐ戻る。"""
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._worker = threading.Thread(
                target=self._stop_current, args=(generation,),
                kwargs={"force": force}, name="stop", daemon=True)
            self._worker.start()

    def _stop_current(self, generation: int, *, force: bool = False) -> bool:
        """現在のツールを止めて、**終了を確認する** (要件定義書 §8.1)。"""
        running = self._current
        if running is None:
            self._set(State.IDLE, "起動していません")
            return True

        name = running.display_name or running.app_id
        # 画面を閉じることから始まるので、そう伝える (要件定義書 §8.2)
        if running.browser_managed and process_manager.is_browser_open(running):
            self._set(State.STOPPING, f"{name}の画面を閉じています...")
        else:
            self._set(State.STOPPING, f"{name}を終了しています...")

        # `process_manager.stop` が中で画面を先に閉じてから
        # バックエンドを止める (要件定義書 §8.3 の処理順序)
        result = process_manager.stop(
            running, force=force,
            timeout=float(app_config.ui_setting("stop_timeout_seconds")))
        if result.browser_closed:
            browser.forget(running.browser_pid)
            self._set(State.STOPPING, f"{name}を終了しています...")

        if result.busy:
            # 実行中の処理がある。**止めずに知らせる** (基盤仕様書 2.8)。
            # 中断してよいかは利用者が決める
            detail = (f"{name}で実行中の処理があります: "
                      + "、".join(result.busy_jobs)
                      + "\n終了するときは「強制終了」を選んでください")
            if result.browser_closed:
                # 画面は先に閉じてある。バックエンドは動いたままなので、
                # **黙っていると「消えた」ように見える**
                detail += ("\n画面は閉じましたが、処理は続いています。"
                           "同じボタンを押すと画面を開き直せます。")
            self._set(State.RUNNING, f"現在：{name}", detail=detail,
                      responding=True)
            return False

        if not result.stopped:
            self._set(State.ERROR, f"{name}を終了できませんでした",
                      detail=(f"{result.message}\n"
                              f"ログ: {app_config.local_dir('logs')}"))
            return False

        self._reap_process()
        self._current = None
        runtime_state.clear()
        log.info("停止完了: %s (%s)", name, result.method)
        if not self._superseded(generation):
            self._set(State.IDLE, f"{name}を終了しました")
        return True

    # `start.bat` の受け皿が終わるのを待つ上限 (秒)
    REAP_WAIT_SEC = 3.0

    def _reap_process(self) -> None:
        """`start.bat` で起こしたプロセスを引き取る。

        引き取らないと、終了済みのプロセスがゾンビとしてPID表に残り、
        「まだ動いている」と誤って判定される。

        業務ツール本体が止まれば、それを起こした `cmd.exe` もふつうは
        一緒に終わる。**終わらないときは落とす** ── 残しておくと、
        見えないコンソールが切り替えのたびに1つずつ増えていく。
        自分で起こしたプロセスなので、照合は要らない。
        """
        proc = self._process
        self._process = None
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
    def _start(self, tool: Tool, generation: int) -> None:
        problem = tool_registry.validate_start_command(tool.start_command)
        if not tool.start_command.strip():
            self._set(State.ERROR, f"{tool.display_name}が設定されていません", tool,
                      detail=("設定画面で start.bat の場所を指定してください。\n"
                              "(ランチャーバーの「設定」ボタン)"))
            return
        if problem:
            self._set(State.ERROR, f"{tool.display_name}を起動できません", tool,
                      detail=f"{problem}\n設定画面で指定し直してください。")
            return

        self._set(State.STARTING, f"{tool.display_name}を起動しています...", tool)
        proc = self._spawn(tool)
        if proc is None:
            return
        self._process = proc

        timeout = float(app_config.ui_setting("start_timeout_seconds"))
        started = time.monotonic()
        # BATが先に落ちたことを、時間切れと区別するための入れ物。
        # 「90秒待った末に時間切れ」より「start.bat が3秒で終了した
        # (戻り値 1)」のほうが、次に何を見ればよいか分かる
        early_exit: dict[str, int] = {}

        def should_stop() -> bool:
            if self._superseded(generation):
                return True
            code = proc.poll()
            if code is not None and code != 0:
                early_exit["code"] = code
                return True
            return False

        def on_progress(elapsed: float, payload: Optional[dict]) -> None:
            if self._superseded(generation):
                return
            stage = health.stage_text(payload)
            message = f"{tool.display_name}を起動しています..."
            if stage:
                message = f"{tool.display_name}: {stage}"
            self._set(State.STARTING, message, tool, elapsed=elapsed)

        payload = health.wait_ready(
            tool.health_url, tool.app_id, timeout=timeout,
            on_progress=on_progress, should_stop=should_stop)

        if self._superseded(generation):
            log.info("起動待ちを打ち切りました: %s", tool.display_name)
            return

        if payload is None:
            self._report_start_failure(tool, proc, early_exit,
                                       time.monotonic() - started)
            return

        running = _running_from_health(tool, payload, launch_pid=proc.pid)
        self._current = running
        log.info("起動完了: %s", running.summary())

        # **ここで初めて画面を開く** (要件定義書 §7.1 / §20)
        self._open_browser(running, running.url or tool.home_url)
        # 画面のPIDまで入った状態で記録する。`stop.bat` は別プロセス
        # なので、書いておかないとそちらから画面を閉じられない
        runtime_state.write(running)

        detail = ""
        if not running.browser_managed:
            detail = ("画面は既定のブラウザーで開きました。\n"
                      "切り替えのときに自動では閉じないので、"
                      "不要になったタブは手で閉じてください。")
        self._set(State.RUNNING, f"現在：{tool.display_name}", tool,
                  detail=detail, elapsed=time.monotonic() - started,
                  responding=True)

    def _spawn(self, tool: Tool) -> Optional[subprocess.Popen]:
        """`start.bat` を実行する (要件定義書 §12.2)。

        ランチャーはツールの起動方法を知らない。BATを呼ぶだけにして、
        Pythonの起こし方は各リポジトリ側に残す。
        """
        command = [tool.start_command] + tool.start_args.split()
        out_path = (app_config.local_dir("logs")
                    / f"tool_{_safe_name(tool.app_id)}.out.log")
        log.info("起動開始: %s — %s (cwd=%s)",
                 tool.display_name, " ".join(command), tool.resolved_work_dir)

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
            self._set(State.ERROR, f"{tool.display_name}を起動できませんでした", tool,
                      detail=(f"start.bat を実行できませんでした: {exc}\n"
                              f"パス: {tool.start_command}"))
            return None
        finally:
            if out is not subprocess.DEVNULL:
                out.close()                   # 子プロセス側は自分の複製を持つ

        return proc

    def _report_start_failure(self, tool: Tool, proc: subprocess.Popen,
                              early_exit: dict, elapsed: float) -> None:
        """起動できなかったことを、**次に何を見ればよいか**まで含めて出す。"""
        out_path = (app_config.local_dir("logs")
                    / f"tool_{_safe_name(tool.app_id)}.out.log")
        lines: list[str] = []

        code = early_exit.get("code")
        if code is not None:
            lines.append(f"start.bat が終了しました (戻り値 {code})。")
            if tool.start_args.strip():
                lines.append(f"起動引数「{tool.start_args}」を受け付けない"
                             "ツールかもしれません。設定画面で空にして"
                             "試してください。")
        elif health.is_port_accepting(tool.port):
            # 待ち受けてはいるが応答が返らない。原因はたいていこれ
            lines.append(f"ポート {tool.port} は待ち受けていますが、"
                         "起動確認の応答がありません。")
            proxies = health.proxy_settings()
            if proxies:
                lines.append("プロキシ設定があります ("
                             + "、".join(f"{k}={v}" for k, v in sorted(proxies.items()))
                             + ")。127.0.0.1 が除外されているか確認してください。")
            lines.append(f"起動確認URL: {tool.health_url}")
        else:
            lines.append(f"{elapsed:.0f}秒 待ちましたが応答がありませんでした。")
            lines.append(f"起動確認URL: {tool.health_url}")

        lines.append(f"ツールの出力: {out_path}")
        lines.append(f"ランチャーのログ: {app_config.local_dir('logs')}")

        log.error("起動失敗: %s — %s", tool.display_name, " / ".join(lines))
        self._set(State.ERROR, f"{tool.display_name}を起動できませんでした", tool,
                  detail="\n".join(lines), elapsed=elapsed)

    # --------------------------------------------------------------
    # 生存監視 (基盤仕様書 2.9)
    # --------------------------------------------------------------
    def poll_health(self) -> bool:
        """現在のツールがまだ応答しているか。画面から定期的に呼ぶ。

        **ブラウザーを閉じたこととツールが落ちたことは別物** なので、
        ここで見るのはツール側だけ。落ちていたら状態を戻し、利用者が
        もう一度押せば起動し直せるようにする (要件定義書 §11)。
        """
        running = self._current
        if running is None or self._status.busy:
            return False

        # 利用者が手で閉じた画面を片付ける
        browser.reap()

        if process_manager.is_running(running):
            # **画面の生死はバックエンドと別に見る** (要件定義書 §11)。
            # 利用者が画面だけ手で閉じても、バックエンドは動いたまま。
            # そのことを表に出す ── 出さないと「終わったつもり」で
            # 残り続ける
            browser_open = process_manager.is_browser_open(running)
            if (not self._status.responding
                    or self._status.browser_open != browser_open):
                if running.browser_managed and not browser_open:
                    log.info("画面が閉じられました（バックエンドは動作中）: %s",
                             running.app_id)
                self._set(State.RUNNING, f"現在：{running.display_name}",
                          responding=True, browser_open=browser_open)
            return True

        log.warning("応答が途切れました: %s", running.summary())
        self._reap_process()
        self._current = None
        runtime_state.clear()
        self._set(State.ERROR, f"{running.display_name}が終了しました",
                  detail=("ツール側が停止したか、応答しなくなりました。\n"
                          "もう一度ボタンを押すと起動し直します。"))
        return False

    # --------------------------------------------------------------
    # 後始末
    # --------------------------------------------------------------
    def shutdown(self, *, stop_tools: bool, force: bool = False) -> bool:
        """ランチャーを終わるとき。止められたかを返す。

        業務ツールまで止めるかは利用者に選ばせる ── ブラウザーを閉じる
        こととバックエンドを止めることは別 (要件定義書 §11)。長い処理の
        途中でランチャーを閉じただけで落とされるのは困る。

        **止まらなかったことを黙って飲み込まない。** 実行中の処理が
        あって止められなかったのに閉じてしまうと、利用者は「止めた
        つもり」で残ったツールに気づけない。
        """
        with self._lock:
            self._generation += 1
        if stop_tools and self._current is not None:
            return self._stop_current(self._generation, force=force)
        self._reap_process()
        return True

    # --------------------------------------------------------------
    def _superseded(self, generation: int) -> bool:
        """自分より新しい要求が来ているか。"""
        return generation != self._generation

    def _open_browser(self, running: RunningTool, url: str) -> None:
        """画面を開き、閉じるための手がかりを記録に残す。

        専用プロファイルのアプリウィンドウとして開けたときだけ、PIDと
        プロファイルの道が入る。既定ブラウザーへ渡しただけのときは
        空のままで、切り替えのときに閉じられないことが記録に残る。
        """
        if not url:
            return
        try:
            session = browser.open_window(running.app_id, url)
        except Exception as exc:              # noqa: BLE001 - 開けなくても続ける
            log.warning("画面を開けませんでした (%s): %s", url, exc)
            return
        running.browser_pid = session.pid
        running.browser_profile = session.profile_dir
        if self._current is running:
            runtime_state.write(running)


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
    return RunningTool(
        app_id=tool.app_id,
        display_name=tool.display_name,
        pid=int(payload.get("pid") or 0),
        launch_pid=launch_pid,
        port=port,
        url=url,
        health_url=tool.health_url,
        app_root=str(payload.get("app_root") or tool.resolved_work_dir),
        start_command=tool.start_command,
        work_dir=tool.resolved_work_dir,
        stop_command=tool.stop_command,
        stop_method=tool.stop_method,
    )


def _safe_name(app_id: str) -> str:
    """アプリIDをファイル名に使える形へ。"""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in app_id)
