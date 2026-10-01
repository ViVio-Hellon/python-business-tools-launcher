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


def close_question(names: str) -> str:
    """ランチャーを閉じるときの問い。**止めずに閉じたらどうなるか**まで書く。"""
    return (f"動いているツール: {names}\n\n"
            "「はい」  … ツールも終了してからランチャーを閉じる\n"
            "「いいえ」… ツールは動かしたまま、ランチャーだけ閉じる\n"
            "「キャンセル」… 閉じない\n\n"
            "動かしたままにしたツールは、そのまま使えます。画面を閉じると\n"
            "そのツールは自分で終了します。次にランチャーを起動したときは、\n"
            "動いているツールを引き継ぎます (二重には起動しません)。")
