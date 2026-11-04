"""パスの比較用の正規化 (1か所だけ)

Windows は大文字と小文字を区別せず、`\\` と `/` が混ざる。起動ファイルの道は
引用符で囲まれて渡ることもある。比べる前に、これでそろえる。

以前は同じ中身の関数が 3 つ (desktop・tool_registry・process_manager) あった。
"""
from __future__ import annotations

import os
import re
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
    # ファイルを選ぶ画面 (tkinter) は「//サーバー/共有/…」と / で返す。cmd には \ で渡す (1.8.13)
    path = path.replace("/", "\\")
    rest = "".join(" " + _quote(a) for a in args)
    if suffix in (".bat", ".cmd"):
        shell = os.environ.get("COMSPEC") or "cmd.exe"
        return f'{_quote(shell)} /d /s /c "{_quote(path, always=True)}{rest}"'
    if suffix == ".vbs":
        return f"wscript.exe {_quote(path, always=True)}{rest}"
    return f"{_quote(path, always=True)}{rest}"


# ------------------------------------------------------------------
# 共有フォルダー (\\サーバー\…) から動かない .bat を見分ける (1.8.17)
# ------------------------------------------------------------------
# cmd は \\サーバー\… を「いまのフォルダー」にできない。`cd /d "%~dp0"` で自分の
# フォルダーへ移る .bat は、共有フォルダーに置くと移れずに C:\Windows に居たまま
# 終わる (NEON COIL ZERO が 0.3 秒・戻り値 1)。`pushd "%~dp0"` なら移れる。
#
# 1.8.13〜1.8.17 はランチャーがドライブ文字を割り当ててから呼んでいたが、実機で
# 確かめないまま入れ、もともと動いていた日報複合ツールの起動確認を壊した。
# **呼び方は 1.8.12 と同じに戻し、見分けて知らせるだけにする** (1.8.18)。
_CD_DP0 = re.compile(r'^\s*cd\s+/d\s+"?%~dp0', re.IGNORECASE | re.MULTILINE)
_PUSHD_DP0 = re.compile(r'^\s*pushd\s+"?%~dp0', re.IGNORECASE | re.MULTILINE)
_help_cache: dict[tuple[str, float], bool] = {}


def is_unc(path: str) -> bool:
    r"""`\\サーバー\共有\…` (または `//サーバー/…`) か。`\\?\` などの特別な形は除く。"""
    text = (path or "").strip().strip('"').replace("/", "\\")
    return text.startswith("\\\\") and not text.startswith(("\\\\?\\", "\\\\.\\"))


def needs_unc_help(path: Union[str, PurePath]) -> bool:
    """共有フォルダーから呼ぶと動かない作りの .bat か。

    `cd /d "%~dp0"` で自分のフォルダーへ移り、`pushd "%~dp0"` を使っていないもの
    だけ。読めないもの・`pushd` を使うもの・どちらも無いものは偽。
    """
    text = str(path).strip().strip('"')
    try:
        stat = os.stat(text)
        key = (normalize(text), stat.st_mtime)
        if key in _help_cache:
            return _help_cache[key]
        with open(text, "rb") as handle:
            data = handle.read(64 * 1024)
    except OSError:
        return False
    body = data.decode("cp932", errors="replace")
    answer = bool(_CD_DP0.search(body)) and not _PUSHD_DP0.search(body)
    _help_cache[key] = answer
    return answer


def unc_bat_problem(path: Union[str, PurePath]) -> str:
    """共有フォルダーに置いた .bat が、そこから動かない書き方なら、その説明。無ければ空。"""
    text = str(path).strip().strip('"')
    if not text.lower().endswith((".bat", ".cmd")) or not is_unc(text):
        return ""
    if not needs_unc_help(text):
        return ""
    name = PurePath(text.replace("\\", "/")).name
    return (f"{name} は `cd /d \"%~dp0\"` で自分のフォルダーへ移る書き方で、共有フォルダー "
            "(\\\\サーバー\\…) に置くと移れずに止まる (cmd は共有フォルダーへ cd できない)。"
            "ツール側で `pushd \"%~dp0\"` に変える (1 行)、EXE で配る、"
            "または各 PC の中に置く")
