"""ランチャー自身の起動 (バーを出す前の下ごしらえ)

`Start.vbs` を押してからバーが出るまで、以前は**何も画面に出なかった**。
作業者には「押したのに何も起きない」としか見えず、もう一度押す。

そこで下ごしらえを段に分け、進み具合を知らせながら進める。画面
(起動中の窓: `launcher/ui/splash.py`) は別スレッドでこれを回し、
知らせを受けて描く。**ここは画面の部品に触らない** ── tkinter の
無い環境でも順番と中身を確かめられるようにするため。

    1. 二重起動していないか確かめる     (launch_guard)
    2. 設定を読み込む                   (設定DB・配布先フォルダ)
    3. 動いているツールを探す           (/api/health を同時に当たる)
    4. バーを準備する

**ダイアログはここでは出さない。** 別スレッドから tkinter を触ると
落ちるので、知らせたいことは `warnings` に入れて返し、起動中の窓を
閉じたあとで呼び出し側が出す。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from launcher import app_config
from launcher.logging_utils import get_logger

log = get_logger("boot")

STEPS = (
    "二重起動していないか確かめています",
    "設定を読み込んでいます",
    "動いているツールを探しています",
    "バーを準備しています",
)
GUARD, SETTINGS, ADOPT, BAR = range(len(STEPS))

Report = Callable[[int], None]


@dataclass
class BootResult:
    # 起動してよいか。多重起動なら False (ロックは取っていない)
    started: bool
    # 起動しなかった理由
    reason: str = ""
    # 起動したときの `ToolManager`
    manager: Any = None
    # 起動中の窓を閉じたあとで出す知らせ。(題名, 本文)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    # 段ごとにかかった秒。遅い端末で**どこが遅いか**を後から追えるように
    timings: list[tuple[str, float]] = field(default_factory=list)


def run(report: Report = lambda step: None, *,
        clock: Callable[[], float] = time.monotonic) -> BootResult:
    """下ごしらえを順に行う。段に入るたびに `report(段の番号)` を呼ぶ。

    多重起動でなければ**ロックを取った状態で**返す。外すのは呼び出し側
    (バーを閉じたとき)。途中で失敗したときは、ここで外してから投げ直す。
    """
    import launch_guard

    result = BootResult(started=False)
    started_at = clock()
    last = [started_at]

    def enter(step: int) -> None:
        now = clock()
        if step > 0:
            result.timings.append((STEPS[step - 1], now - last[0]))
        last[0] = now
        report(step)

    # --- 1. 二重起動 (基盤仕様書 2.4) ---
    # **調べてから書くのではなく、取れたら起動する。**
    enter(GUARD)
    guard = launch_guard.acquire()
    if not guard.should_start:
        result.reason = guard.reason
        log.info("多重起動のため終了します: %s", guard.reason)
        return result
    log.info("多重起動の判定: %s", guard.reason)
    result.started = True

    try:
        enter(SETTINGS)
        _load_settings(result)

        enter(ADOPT)
        from app_manager import ToolManager

        manager = ToolManager()
        # すでに動いているツールがあれば引き継ぐ (要件定義書 §9)
        manager.adopt_running()
        result.manager = manager

        enter(BAR)
        # 画面の部品を読み込んでおく。窓を作るのは呼び出し側 (メインスレッド)。
        # 読めなくてもここでは止めない ── 呼び出し側がもう一度読み込み、
        # そこで理由つきで失敗する (tkinter の無い試験環境でも段を確かめられる)
        try:
            import launcher.ui.bar  # noqa: F401
        except ImportError:
            pass

        result.timings.append((STEPS[BAR], clock() - last[0]))
    except BaseException:
        launch_guard.remove_lock()
        raise

    log.info("起動の内訳: %s (合計 %.2f秒)",
             " / ".join(f"{name} {sec:.2f}秒" for name, sec in result.timings),
             clock() - started_at)
    return result


def _load_settings(result: BootResult) -> None:
    """設定DBと配布先フォルダ。読めないものがあれば知らせを積む。"""
    from launcher import distribution, tool_registry

    # 設定ファイルが壊れていたら、**黙って進めない**。既定値で動くが、
    # 利用者には「ランチャーが壊れている」としか見えないので理由を出す
    problem = app_config.load_error()
    if problem:
        result.warnings.append((
            "設定ファイルを読めません",
            f"{problem}\n\n"
            "既定の設定で起動します (ツールの設定は設定DBにあるので消えません)。\n"
            "config/launcher.json を配布元のものに戻してください。\n\n"
            "よくある原因: JSON にコメント (//) を書いた、"
            "カンマの過不足、メモ帳で別の文字コードで保存した"))

    # 配布先フォルダがあれば読む (`tool_registry.initialize`)。壊れていても
    # 起動は止めない ── その端末の設定で動く
    _, problem = distribution.load()
    if problem:
        log.warning("%s", problem)
        result.warnings.append((
            "配布先フォルダの設定を読めません",
            f"{problem}\n\n"
            "この端末の設定のまま起動します。\n"
            "配布元の端末で［設定］→「配布先フォルダを作る」から"
            "作り直してください。"))
    _, problem = distribution.load_password()
    if problem:
        log.warning("%s", problem)
        result.warnings.append((
            "管理者パスワードのファイルを読めません",
            f"{problem}\n\n"
            "パスワードを確かめられないため、［設定］は開けません。\n"
            f"{distribution.PASSWORD_FILE} を消すと、次に［設定］を開くときに"
            "決め直せます (ツールの設定は消えません)。"))

    tool_registry.initialize()

    # 登録が無くなったツールの画面プロファイルを片付ける。
    # 起動時に1度だけ ── 消し忘れたキャッシュが端末に溜まらないように
    try:
        from launcher import browser

        browser.purge_unused(t.app_id for t in
                             tool_registry.all_tools(include_disabled=True))
    except Exception:                         # noqa: BLE001 - 片付けで起動を止めない
        log.warning("画面プロファイルの片付けに失敗しました", exc_info=True)
