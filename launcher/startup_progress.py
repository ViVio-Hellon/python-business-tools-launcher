"""起動・切り替えの進み具合 (画面に出す中身)

バーの1行だけでは、ツールが立ち上がるまでの数十秒が「何も起きて
いない」ように見える。**いまどの段にいて、何が済んだか**を別の窓に
出すための中身をここで組み立てる。

    日報 → 看板 に切り替えています
      ✓ 日報の画面を閉じる
      ✓ 日報を終了する
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
    窓を閉じる合図。**1回の起動・切り替えを通して**、どの段を通って
    きたかを覚えておき、済んだ段に印を付ける。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._reset()

    def _reset(self) -> None:
        self._started: Optional[float] = None
        self._previous = ""       # 止めているツール
        self._target = ""         # 起こすツール
        self._closes_browser = False
        self._last = None

    def update(self, status) -> Optional[ProgressView]:
        if not getattr(status, "busy", False):
            self._reset()
            return None

        phase = getattr(status, "phase", "")
        if phase in STOP_PHASES:
            target = getattr(status, "target_name", "")
            if (self._started is not None
                    and (status.display_name != self._previous
                         or target != self._target)):
                self._reset()             # 別の操作が始まった
            self._previous = status.display_name
            self._target = target
            if phase == CLOSE_BROWSER:
                self._closes_browser = True
        elif phase in START_PHASES:
            if self._started is not None and status.display_name != self._target:
                self._reset()             # 起こすツールが変わった
            self._target = status.display_name

        if self._started is None:
            self._started = self._clock()
        self._last = status
        return self.view()

    def view(self) -> Optional[ProgressView]:
        """いまの中身。経過秒だけ進めたいときにも呼ぶ。"""
        status = self._last
        if status is None:
            return None
        phase = getattr(status, "phase", "")

        plan: list[tuple[str, str]] = []
        if self._previous:
            if self._closes_browser:
                plan.append((CLOSE_BROWSER, f"{self._previous}の画面を閉じる"))
            plan.append((STOP_TOOL, f"{self._previous}を終了する"))
        if self._target:
            plan.append((SPAWN, f"{self._target}の起動ファイルを実行する"))
            plan.append((WAIT, f"{self._target}の準備ができるのを待つ"))
            plan.append((OPEN_BROWSER, f"{self._target}の画面を開く"))

        order = [key for key, _ in plan]
        current = order.index(phase) if phase in order else -1
        steps = tuple(
            Step(label, DONE if i < current else
                 ACTIVE if i == current else PENDING)
            for i, (_, label) in enumerate(plan))

        if self._previous and self._target:
            title = f"{self._previous} → {self._target} に切り替えています"
        elif self._target:
            title = f"{self._target}を起動しています"
        elif self._previous:
            title = f"{self._previous}を終了しています"
        else:
            title = status.message or "処理しています"

        elapsed = max(0, int(self._clock() - (self._started or self._clock())))
        elapsed_text = f"{elapsed}秒"
        timeout = int(getattr(status, "timeout", 0) or 0)
        if phase == WAIT and timeout:
            elapsed_text += f" (最大{timeout}秒まで待ちます)"

        note = getattr(status, "stage", "") if phase == WAIT else ""
        return ProgressView(title=title, steps=steps, note=note,
                            elapsed_text=elapsed_text)
