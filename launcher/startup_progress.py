"""起動・停止の進み具合 (画面に出す中身)

バーの1行だけでは、ツールが立ち上がるまでの数十秒が「何も起きて
いない」ように見える。**いまどの段にいて、何が済んだか**を別の窓に
出すための中身をここで組み立てる。

    看板を起動しています
      ✓ 看板の起動ファイルを実行する
      ▶ 看板の準備ができるのを待つ   アプリを準備中
      ・ 看板の画面を開く
      12秒 (最大90秒まで待ちます)

**画面の部品には触らない** (`ui/progress_window.py` が描く)。
tkinter の無い環境でも中身を確かめられるようにするため。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

# `app_manager` の段と同じ値。こちらから `app_manager` は読まない
# (画面寄りの部品が起動制御を読み込む形にしない)
CLOSE_BROWSER = "close_browser"
STOP_TOOL = "stop_tool"
SPAWN = "spawn"
WAIT = "wait"
OPEN_BROWSER = "open_browser"

STOP_PHASES = (CLOSE_BROWSER, STOP_TOOL)
START_PHASES = (SPAWN, WAIT, OPEN_BROWSER)

DONE = "done"
ACTIVE = "active"
PENDING = "pending"


@dataclass(frozen=True)
class Step:
    label: str
    state: str          # DONE / ACTIVE / PENDING


@dataclass(frozen=True)
class ProgressView:
    title: str
    steps: tuple[Step, ...]
    # ツールが返した準備の段階 (「アプリを準備中」など)。無ければ空
    note: str
    elapsed_text: str


class ProgressTracker:
    """状態の通知を受けて、進み具合の中身を返す。

    忙しくない (起動中でも終了中でもない) 通知が来たら `None` ──
    窓を閉じる合図。**1回の起動・停止を通して**、どの段を通って
    きたかを覚えておき、済んだ段に印を付ける。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._reset()

    def _reset(self) -> None:
        self._started: Optional[float] = None
        self._app_id = ""         # 見ている操作のツール
        self._name = ""
        self._kind = ""           # "start" か "stop"
        self._closes_browser = False
        # 自分の窓を出すアプリか。ブラウザーを開く段が無い
        self._app = False
        self._last = None

    @property
    def watching(self) -> str:
        """いま見ている操作のツール (アプリID)。見ていなければ空。"""
        return self._app_id

    def update(self, status) -> Optional[ProgressView]:
        """知らせを1つ受け取る。窓に出す中身か、閉じる合図の `None`。

        ツールは同時に複数動くので、**見ている操作が終わるまで、ほかの
        ツールの知らせは混ぜない** (混ぜると、窓が2つのツールのあいだを
        行ったり来たりする)。
        """
        app_id = getattr(status, "app_id", "")
        if self._app_id and app_id != self._app_id:
            return self.view()

        if not getattr(status, "busy", False):
            self._reset()
            return None

        phase = getattr(status, "phase", "")
        kind = ("stop" if phase in STOP_PHASES
                else "start" if phase in START_PHASES else "")
        if self._started is not None and kind and kind != self._kind:
            self._reset()                 # 同じツールで、止めてから起こした
        if self._started is None:
            self._started = self._clock()
            self._app_id = app_id
            self._name = getattr(status, "display_name", "") or app_id
            self._kind = kind
            self._app = getattr(status, "ui_mode", "") == "app"
        if phase == CLOSE_BROWSER:
            self._closes_browser = True
        self._last = status
        return self.view()

    def view(self) -> Optional[ProgressView]:
        """いまの中身。経過秒だけ進めたいときにも呼ぶ。"""
        status = self._last
        if status is None:
            return None
        phase = getattr(status, "phase", "")
        name = self._name

        plan: list[tuple[str, str]] = []
        if self._kind == "stop":
            if self._closes_browser:
                plan.append((CLOSE_BROWSER, f"{name}の画面を閉じる"))
            plan.append((STOP_TOOL, f"{name}を閉じる" if self._app
                         else f"{name}を終了する"))
            title = f"{name}を終了しています"
        elif self._kind == "start" and self._app:
            # アプリの窓が画面。ブラウザーは開かない
            plan.append((SPAWN, f"{name}を起動する"))
            plan.append((WAIT, f"{name}の起動を確かめる"))
            title = f"{name}を起動しています"
        elif self._kind == "start":
            plan.append((SPAWN, f"{name}の起動ファイルを実行する"))
            plan.append((WAIT, f"{name}の準備ができるのを待つ"))
            plan.append((OPEN_BROWSER, f"{name}の画面を開く"))
            title = f"{name}を起動しています"
        else:
            title = status.message or "処理しています"

        order = [key for key, _ in plan]
        current = order.index(phase) if phase in order else -1
        steps = tuple(
            Step(label, DONE if i < current else
                 ACTIVE if i == current else PENDING)
            for i, (_, label) in enumerate(plan))

        elapsed = max(0, int(self._clock() - (self._started or self._clock())))
        elapsed_text = f"{elapsed}秒"
        timeout = int(getattr(status, "timeout", 0) or 0)
        if phase == WAIT and timeout:
            elapsed_text += f" (最大{timeout}秒まで待ちます)"

        note = getattr(status, "stage", "") if phase == WAIT else ""
        return ProgressView(title=title, steps=steps, note=note,
                            elapsed_text=elapsed_text)
