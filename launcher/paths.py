"""パスの比較用の正規化 (1か所だけ)

Windows は大文字と小文字を区別せず、`\\` と `/` が混ざる。起動ファイルの道は
引用符で囲まれて渡ることもある。比べる前に、これでそろえる。

以前は同じ中身の関数が 3 つ (desktop・tool_registry・process_manager) あった。
"""
from __future__ import annotations


def normalize(path: str) -> str:
    """比べるための形。前後の空白と引用符を取り、`/` にそろえ、末尾の `/` を
    取り、小文字にする。空なら空。"""
    return (path or "").strip().strip('"').replace("\\", "/").rstrip("/").lower()
