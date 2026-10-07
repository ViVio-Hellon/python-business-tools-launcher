"""バーに出す文 (tkinter を使わない)

画面の部品から切り離しておき、tkinter の無い環境でも中身を確かめる。
"""
from __future__ import annotations


def fit_text(text: str, width: int, measure) -> str:
    """`width` (px) に収まるよう、後ろを「…」にして切る。"""
    if measure(text) <= width:
        return text
    ellipsis = "…"
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if measure(text[:middle] + ellipsis) <= width:
            low = middle
        else:
            high = middle - 1
    return text[:low] + ellipsis


# ランチャーを閉じるときに、利用者の答えからすること
CLOSE_CANCEL = "cancel"     # 閉じない
CLOSE_KEEP = "keep"         # ツールは動かしたまま、ランチャーだけ閉じる
CLOSE_STOP = "stop"         # ツールも終了してから閉じる

# 問いの既定のボタン。**「はい」(全部止める) にしない** ── うっかり Enter を
# 押しただけで、動いているツールがすべて止まる。「キャンセル」なら何も起きない
CLOSE_DEFAULT = "cancel"


def close_question(names: str, starting: str = "") -> str:
    """ランチャーを閉じるときの問い。**止めずに閉じたらどうなるか**まで書く。

    `starting` は起動している最中のツール。以前は数に入れておらず、
    起動の最中に閉じると**何も聞かずに**起動をやめていた。
    """
    lines = []
    if names:
        lines.append(f"動いているツール: {names}")
    if starting:
        lines.append(f"起動中のツール: {starting}")
    stop_note = "（起動中のものは起動をやめる）" if starting else ""
    keep_note = "（起動中のものはそのまま起動を続ける）" if starting else ""
    return ("\n".join(lines) + "\n\n"
            f"「はい」  … ツールも終了してからランチャーを閉じる{stop_note}\n"
            f"「いいえ」… ツールは動かしたまま、ランチャーだけ閉じる{keep_note}\n"
            "「キャンセル」… 閉じない\n\n"
            "動かしたままにしたツールは、そのまま使えます。画面を閉じると\n"
            "そのツールは自分で終了します。次にランチャーを起動したときは、\n"
            "動いているツールを引き継ぎます (二重には起動しません)。")


def close_choice(answer) -> str:
    """問いの答えを、することに直す。

    **「はい」とはっきり答えたときだけ止める。** 答えが真偽値でない形で
    返ってきても (`"no"` という文字など)、`bool()` で読むと「はい」扱いに
    なってしまう。分からない答えは「止めない」に倒す。
    """
    if answer is None:
        return CLOSE_CANCEL
    if answer is True or str(answer).strip().lower() == "yes":
        return CLOSE_STOP
    return CLOSE_KEEP


def force_question(detail: str) -> str:
    """止めようとして断られたあとの問い (中断してでも止めるか)。"""
    return (f"{detail}\n\n"
            "実行中の処理を中断して、ツールを止めてから閉じますか?\n\n"
            "「はい」  … 中断して止め、ランチャーを閉じる"
            " (保存していない内容や途中の処理は失われます)\n"
            "「いいえ」… 中断しない。ツールもランチャーもそのまま")
