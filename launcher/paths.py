"""パスの比較用の正規化 (1か所だけ)

Windows は大文字と小文字を区別せず、`\\` と `/` が混ざる。起動ファイルの道は
引用符で囲まれて渡ることもある。比べる前に、これでそろえる。

以前は同じ中身の関数が 3 つ (desktop・tool_registry・process_manager) あった。
"""
from __future__ import annotations

import os
import re
from pathlib import PurePath
from typing import Iterable, Optional, Union


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
        wrapper = ""
        if is_unc(path) and needs_unc_help(path):
            # `cd /d "%~dp0"` で始まる .bat だけ (共有フォルダーでは動かなかったもの)。
            # **pushd を使う .bat は 1.8.12 までと同じに呼ぶ** (1.8.13 で全部を
            # ドライブ文字の道で呼び、日報複合ツールの起動確認が「指定されたパスが
            # 見つかりません」で動かなくなった。1.8.17)
            path = mapped(path) or path
            wrapper = unc_wrapper() if is_unc(path) else ""
        if wrapper:
            # 共有フォルダーの .bat は包みを通す (下の UNC_WRAPPER を見る)
            return (f'{_quote(shell)} /d /s /c "{_quote(wrapper, always=True)} '
                    f'{_quote(path, always=True)}{rest}"')
        return f'{_quote(shell)} /d /s /c "{_quote(path, always=True)}{rest}"'
    if suffix == ".vbs":
        return f"wscript.exe {_quote(path, always=True)}{rest}"
    return f"{_quote(path, always=True)}{rest}"


# ------------------------------------------------------------------
# 共有フォルダー (\\サーバー\…) の .bat を動かす包み (1.8.13)
# ------------------------------------------------------------------
# cmd は \\サーバー\… を「いまのフォルダー」にできない。起動ファイルが
# `cd /d "%~dp0"` で自分のフォルダーへ移る作りだと、移れずに C:\Windows に居たまま
# 「main.py が見つからない」で終わる (neon-coil-zero が 0.3 秒・戻り値 1 で終わった)。
#
# * その共有フォルダーに**ドライブ文字がすでにあれば**、その道 (Z:\…) で呼ぶ
# * 無ければ包みを通す。包みは `pushd` で一時のドライブ文字を割り当て、その道で
#   起動ファイルを呼ぶ (中の %~dp0 が Z:\… になり、cd /d が通る)。
#   **割り当ては外さない** ── 起動ファイルが裏でサーバーを起こしてすぐ終わる作りだと、
#   外すと動いているサーバーの足元が消える。次からは上の「すでにあれば」で使い回す
#   ので、共有フォルダーごとに 1 つで済む
# * 戻り値はそのまま返す (stop.bat の「処理中」を読むため)
UNC_WRAPPER_NAME = "launcher_unc_call.bat"
UNC_WRAPPER = "\r\n".join([
    "@echo off",
    "rem Business Tools Launcher: runs a .bat that is on a shared folder (\\\\server\\...).",
    "rem cmd cannot use \\\\server\\... as the current folder, so map it with pushd",
    "rem and call the .bat through the mapped drive letter. The drive is kept so that",
    "rem programs started in the background keep working; the launcher reuses it.",
    "pushd \"%~dp1\" || exit /b 1",
    "call \"%CD%\\%~nx1\" %2 %3 %4 %5 %6 %7 %8 %9",
    "exit /b %ERRORLEVEL%",
    "",
])

_DRIVE_CACHE_SEC = 5.0
_drive_cache: tuple[float, dict[str, str]] = (0.0, {})


def is_unc(path: str) -> bool:
    r"""`\\サーバー\共有\…` (または `//サーバー/…`) か。`\\?\` などの特別な形は除く。"""
    text = (path or "").strip().strip('"').replace("/", "\\")
    return text.startswith("\\\\") and not text.startswith(("\\\\?\\", "\\\\.\\"))


def network_drives() -> dict[str, str]:
    """共有フォルダーに割り当てたドライブ文字 (`"z:"` → `"//サーバー/共有"`、比べる形)。

    Windows だけ (ほかは空)。`pushd` が割り当てたものも入る。見回りから何度も
    呼ばれるので 5 秒だけ覚えておく。
    """
    global _drive_cache
    if os.name != "nt":
        return {}
    import time

    now = time.monotonic()
    if now - _drive_cache[0] < _DRIVE_CACHE_SEC:
        return _drive_cache[1]
    found: dict[str, str] = {}
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32           # type: ignore[attr-defined]
        mpr = ctypes.WinDLL("mpr")                  # type: ignore[attr-defined]
        mask = kernel32.GetLogicalDrives()
        for index in range(26):
            if not mask & (1 << index):
                continue
            letter = chr(ord("A") + index) + ":"
            if kernel32.GetDriveTypeW(letter + "\\") != 4:     # DRIVE_REMOTE
                continue
            buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buffer))
            if mpr.WNetGetConnectionW(letter, buffer, ctypes.byref(size)) == 0 and buffer.value:
                found[letter.lower()] = normalize(buffer.value)
    except Exception:                         # noqa: BLE001 - 分からなければ使わないだけ
        found = {}
    _drive_cache = (now, found)
    return found


def mapped(path: str) -> str:
    """共有フォルダーの道を、割り当て済みのドライブ文字の道にする。無ければ空。"""
    if not is_unc(path):
        return ""
    text = str(path).strip().strip('"').replace("/", "\\")
    wanted = normalize(text)
    for letter, root in sorted(network_drives().items()):
        if root and (wanted == root or wanted.startswith(root + "/")):
            return letter.upper() + "\\" + text[len(root):].lstrip("\\")
    return ""


def as_unc(text: str) -> str:
    """比べる形 (`normalize`) の中のドライブ文字の道を、共有フォルダーの道に戻す。

    `pushd` で割り当てたドライブ文字 (Z:\…) から起動したツールのプロセスも、
    共有フォルダーの道 (\\サーバー\…) で登録したツールのものと見分けるため。
    """
    drives = network_drives()
    if not drives or not text:
        return text
    import re

    for letter, root in drives.items():
        text = re.sub(rf"(?<![a-z0-9]){re.escape(letter)}/", lambda _m, r=root: r + "/", text)
        if text.endswith(letter) and (len(text) == 2 or not text[-3].isalnum()):
            text = text[:-2] + root
    return text


_CD_DP0 = re.compile(r'^\s*cd\s+/d\s+"?%~dp0', re.IGNORECASE | re.MULTILINE)
_PUSHD_DP0 = re.compile(r'^\s*pushd\s+"?%~dp0', re.IGNORECASE | re.MULTILINE)
_help_cache: dict[tuple[str, float], bool] = {}


def needs_unc_help(path: Union[str, PurePath]) -> bool:
    """共有フォルダーから呼ぶと動かない作りの .bat か (1.8.17)。

    `cd /d "%~dp0"` で自分のフォルダーへ移り、`pushd "%~dp0"` を使っていないもの
    だけ。読めないもの・`pushd` を使うもの・どちらも無いものは**偽** (呼び方を変えない)。
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


def contains_path(haystack: str, needle: str) -> bool:
    """比べる形 (`normalize`) どうしで、`haystack` が `needle` を含むか。

    **ドライブ文字の道と共有フォルダーの道を同じものとして見る** (両方を
    `as_unc` でそろえて比べる)。片方だけそろえると、ドライブ文字 (S:\…) で
    登録したツールが、自分のプロセスを見分けられなくなる (1.8.17)。
    """
    if not needle:
        return False
    if needle in haystack:
        return True
    if not network_drives():
        return False
    return as_unc(needle) in as_unc(haystack)


def entry_cwd(path: Union[str, PurePath], cwd: Optional[str]) -> Optional[str]:
    r"""起動ファイルを動かすときの作業フォルダー。

    cmd は共有フォルダーを作業フォルダーにできない (「UNC パスはサポートされません」と
    出して C:\Windows で動く。その文が stop.bat の答えの 1 行目に混ざる)。.bat の
    ときは、ドライブ文字があればその道、無ければこの PC の中のフォルダーにする
    (包みが自分で移る)。
    """
    if os.name != "nt" or not cwd or not is_unc(cwd):
        return cwd
    if PurePath(str(path).strip().strip('"')).suffix.lower() not in (".bat", ".cmd"):
        return cwd
    if not needs_unc_help(path):
        return cwd                            # 1.8.12 までと同じ (1.8.17)
    drive = mapped(cwd)
    if drive:
        return drive
    try:
        from . import app_config

        folder = app_config.local_dir("runtime")
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder)
    except Exception:                         # noqa: BLE001
        return cwd


def unc_wrapper() -> str:
    """包みの .bat を**この PC の中**に置いて、その道を返す。置けなければ空 (そのまま呼ぶ)。"""
    try:
        from . import app_config

        folder = app_config.local_dir("runtime")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / UNC_WRAPPER_NAME
        data = UNC_WRAPPER.encode("ascii")
        if not target.is_file() or target.read_bytes() != data:
            target.write_bytes(data)
        return str(target)
    except Exception:                         # noqa: BLE001 - 置けなくても起動は試す
        return ""
