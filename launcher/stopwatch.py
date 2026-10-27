"""待つ時間の計り方 — **PC のスリープ中の時間は数えない** (1.7.13)

ランチャーは「ツールが答えるまで最大 90 秒待つ」のように、時間で決める
所がある。Windows の Python の時計 (`time.monotonic`) は**スリープ中も進む**
ので、待っているあいだに PC が眠ると、復帰した瞬間に時間切れになる。
起動を待っていたなら「起動できませんでした」と出し、立ち上がりかけの
ツールのプロセスまで止めていた (他ツールの「スリープ明けにサーバーが
誤って終わる」と同じ形)。

そこで、計るたびに前回からの間隔を見て、**大きく飛んでいたらスリープ
していたとみなし、その分は数えない**。ふつうの待ち方は 1 回が長くても
十数秒 (起動確認の入口は 10 秒まで) なので、それより十分長い飛びだけを
スリープとみなす。スリープ中に時計が止まる環境 (Linux など) では飛ば
ないので、そのまま正しく数える。
"""
from __future__ import annotations

import time

# これより長く間が空いたら、スリープしていたとみなす (秒)。待ち方の 1 回
# (起動確認の入口 10 秒・応答の確認 1.5 秒) より十分長くしておく
SLEEP_GAP_SEC = 30.0


class Stopwatch:
    """経過時間。**大きく飛んだ分 (スリープ) は数えない。**

    `elapsed()` を呼ぶたびに前回からの間隔を見る。待つ処理の 1 回ごとに
    呼べば、スリープをまたいだ間隔を見つけられる。
    """

    def __init__(self, *, gap: float = SLEEP_GAP_SEC) -> None:
        self.gap = gap
        self._started = self._last = time.monotonic()
        self.slept = 0.0          # 数えなかった時間の合計 (秒)
        self.sleeps = 0           # スリープとみなした回数

    def elapsed(self) -> float:
        now = time.monotonic()
        step = now - self._last
        self._last = now
        if step > self.gap:
            self.slept += step
            self.sleeps += 1
        return max(0.0, now - self._started - self.slept)

    @property
    def crossed_sleep(self) -> bool:
        """待っているあいだに PC がスリープしたか。"""
        return self.sleeps > 0

    def note(self) -> str:
        """スリープをまたいだときの一文 (無ければ空)。"""
        if not self.sleeps:
            return ""
        span = (f"約 {self.slept:.0f} 秒" if self.slept < 120
                else f"約 {self.slept / 60:.0f} 分")
        return f"待っているあいだに PC がスリープしていました ({span}。その時間は数えていません)。"
