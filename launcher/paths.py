"""パスの比較用の正規化 (1か所だけ)

Windows は大文字と小文字を区別せず、`\\` と `/` が混ざる。起動ファイルの道は
引用符で囲まれて渡ることもある。比べる前に、これでそろえる。

以前は同じ中身の関数が 3 つ (desktop・tool_registry・process_manager) あった。
"""
from __future__ import annotations

import os
from pathlib import PurePath
from typing import Iterable, Union


def normalize(path: str) -> str:
    """比べるための形。前後の空白と引用符を取り、`/` にそろえ、末尾の `/` を
    取り、小文字にする。空なら空。"""
    return (path or "").strip().strip('"').replace("\\", "/").rstrip("/").lower()


# ------------------------------------------------------------------
# 起動ファイル (bat / vbs / exe) を実行するコマンド (1.8.6)
# ------------------------------------------------------------------
# cmd が区切りや指示として読む文字。これを含む引数は " で囲む
_CMD_SPECIAL = set('&|<>^()@,;=%!"')


def _quote(arg: str, *, always: bool = False) -> str:
    """Windows のコマンドラインの1語にする。

    **全角の空白 (U+3000) も空白として扱う。** Python の `subprocess` は半角の
    空白とタブがあるときしか " で囲まない。日本語の cmd は全角の空白でも区切るので、
    フォルダー名「コイルライン用　包装仕様表示」の stop.bat が
    「'…\\コイルライン用' は、内部コマンドまたは外部コマンド…として認識されていません」
    で動かなかった。
    """
    text = str(arg)
    if always or not text or any(ch.isspace() or ch in _CMD_SPECIAL for ch in text):
        return '"' + text.replace('"', "") + '"'
    return text


def entry_command(path: Union[str, PurePath],
                  args: Iterable[str] = ()) -> Union[str, list[str]]:
    """起動ファイル・停止ファイル・起動確認の入口を実行するコマンド。

    Windows では**コマンドラインの文字列**を返す (道はいつも " で囲む):

    * `.bat` / `.cmd` … `cmd.exe /d /s /c ""<道>" 引数"`。/s で外側の " だけを外し、
      中の " はそのまま渡る (空白・括弧・& のある道でも、一語として読まれる)
    * `.vbs` … `wscript.exe "<道>" 引数` (`CreateProcess` は関連付けを解決しない)
    * `.exe` … `"<道>" 引数`

    ほかの OS (試験) では、これまでどおりの引数のリスト。
    """
    path = str(path).strip().strip('"')
    args = [str(a) for a in args]
    suffix = PurePath(path).suffix.lower()
    if os.name != "nt":
        return (["wscript.exe", path, *args] if suffix == ".vbs" else [path, *args])
    rest = "".join(" " + _quote(a) for a in args)
    if suffix in (".bat", ".cmd"):
        shell = os.environ.get("COMSPEC") or "cmd.exe"
        return f'{_quote(shell)} /d /s /c "{_quote(path, always=True)}{rest}"'
    if suffix == ".vbs":
        return f"wscript.exe {_quote(path, always=True)}{rest}"
    return f"{_quote(path, always=True)}{rest}"
