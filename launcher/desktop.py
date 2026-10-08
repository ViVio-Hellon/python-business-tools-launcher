"""自分の窓を持つアプリ (exe) の窓とプロセス

Tauri (Rust) などで作ったツールは、ブラウザーではなく**自分の窓**を出す。
Webサーバーを持たないアプリには `/api/health` も無い。ランチャーは
そうしたツールを、窓とプロセスで扱う:

    起動の確認   … 窓が出たか
    前に出す     … 動いているツールのボタンを押したとき
    閉じる       … 止めるとき。**まず窓を閉じてもらう** (アプリが保存の
                   確認を出せる。いきなりプロセスを落とさない)
    見つける     … ランチャーの外で起動されていたアプリを探す (二重起動しない)
    見分ける     … そのPIDが本当にそのアプリか (PIDは使い回される)

**起動した exe がすぐ終わることがある。** 起動用の exe が本体 (サーバーや
画面) を別のプロセスとして起こし、自分は戻り値 0 で終わる作り。起こした
exe だけを見ていると「終わった」「窓が出ない」と誤る。そこで、
**ツールのフォルダーから起動したプロセス** (実行ファイルかコマンドラインが
そのフォルダーの中) と、その子・孫をまとめて「そのツール」として見る
(`related_pids`)。サーバーがどのポートで待ち受けているかも、そこから分かる
(`listening_ports`)。

Windows の API を**標準ライブラリの ctypes だけ**で呼ぶ。外部コマンド
(PowerShell・wmic・tasklist) を使わないので、それらを禁じている端末でも
動き、5秒ごとの見回りでも重くならない。

Windows 以外 (開発機・試験) には窓が無い。答えられないものは `None` を
返し、呼び出し側が「プロセスが生きているか」で代える。プロセスの親子と
コマンドラインは `/proc` から読む。

**画面の部品 (tkinter) は読み込まない。**
"""
from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from typing import Iterable, Optional, Union

from .logging_utils import get_logger

log = get_logger("desktop")

IS_WINDOWS = os.name == "nt"

# 実行ファイルの中を探すときの上限。Tauri の exe は数MB〜数十MB
_SCAN_LIMIT = 256 * 1024 * 1024
_SCAN_CHUNK = 4 * 1024 * 1024

# Windows の値
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_WM_CLOSE = 0x0010
_SW_RESTORE = 9
_GW_OWNER = 4
_PROCESS_COMMAND_LINE_INFORMATION = 60
_AF_INET6 = 23
_TCP_TABLE_OWNER_PID_LISTENER = 3

Pids = Union[int, Iterable[int]]


# ------------------------------------------------------------------
# 戻り値の読み方
# ------------------------------------------------------------------
# アプリが落ちたときの戻り値。**数字だけでは次に何を見ればよいか分からない**
# ので、よく出るものに意味を添える (なぜなぜの「なぜ」に入る)
_EXIT_MEANINGS = {
    1: "一般的なエラー",
    2: "起動引数の誤り (受け付けない引数を渡した)",
    101: "Rust の panic (Tauri・Rust 製アプリの想定外のエラー)",
    0xC0000005: "アクセス違反 (アプリの不具合・壊れたファイル)",
    0xC0000017: "メモリ不足",
    0xC0000135: "必要な DLL が見つからない (ランタイムが入っていない・ファイルが欠けている)",
    0xC0000142: "DLL の初期化に失敗した",
    0xC000013A: "Ctrl+C などで中断された",
    0xC0000409: "致命的なエラーで即時終了した (スタック破壊・FailFast)",
    0xC0000374: "ヒープ破損",
    0xE0434352: ".NET の想定外の例外",
}


def describe_exit_code(code: Optional[int]) -> str:
    """戻り値を人が読める形に。`3221225477 (0xC0000005: アクセス違反…)`。"""
    if code is None:
        return "不明"
    if -255 <= code < 0 and not IS_WINDOWS:
        # Windows 以外: シグナルで止まった (-15 = SIGTERM、-9 = SIGKILL)
        return f"{code} (シグナル {-code} で終了)"
    value = code & 0xFFFFFFFF
    meaning = _EXIT_MEANINGS.get(value, "")
    if value >= 0x80000000:
        text = f"{code} (0x{value:08X}"
        return text + (f": {meaning})" if meaning else ")")
    return f"{code} ({meaning})" if meaning else str(code)


# ------------------------------------------------------------------
# 実行ファイルを見る
# ------------------------------------------------------------------
def looks_like_tauri(path: str | Path) -> bool:
    """Tauri で作った exe か。

    Tauri の exe には、Rust のクレート名 `tauri` が文字列として多数
    埋め込まれている (`tauri://`・`__TAURI__`・エラー時のソースの道)。
    中を探して見分ける。**外れても困らない** ── 設定画面で「アプリの窓」を
    選び直せる。
    """
    target = Path(str(path).strip().strip('"'))
    if target.suffix.lower() != ".exe":
        return False
    mark = b"tauri"
    tail = b""
    read = 0
    try:
        with open(target, "rb") as handle:
            while read < _SCAN_LIMIT:
                chunk = handle.read(_SCAN_CHUNK)
                if not chunk:
                    return False
                read += len(chunk)
                if mark in (tail + chunk).lower():
                    return True
                tail = chunk[-(len(mark) - 1):]
    except OSError:
        return False
    return False


def file_description(path: str | Path) -> str:
    """exe の版情報にある名前 (「ファイルの説明」→「製品名」)。無ければ空。

    Tauri は `productName` をここへ入れる。［＋ ツールを追加］で exe を
    選んだとき、表示名の候補にする。
    """
    if not IS_WINDOWS:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        version = ctypes.WinDLL("version", use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                                wintypes.DWORD, ctypes.c_void_p]
        version.GetFileVersionInfoW.restype = wintypes.BOOL
        version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                           ctypes.POINTER(ctypes.c_void_p),
                                           ctypes.POINTER(wintypes.UINT)]
        version.VerQueryValueW.restype = wintypes.BOOL

        name = str(path).strip().strip('"')
        handle = wintypes.DWORD()
        size = version.GetFileVersionInfoSizeW(name, ctypes.byref(handle))
        if not size:
            return ""
        data = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(name, 0, size, data):
            return ""

        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version.VerQueryValueW(data, "\\VarFileInfo\\Translation",
                                      ctypes.byref(pointer), ctypes.byref(length)):
            return ""
        if length.value < 4 or not pointer.value:
            return ""
        words = ctypes.cast(pointer, ctypes.POINTER(wintypes.WORD))
        language, codepage = words[0], words[1]
        for key in ("FileDescription", "ProductName"):
            query = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{key}"
            if version.VerQueryValueW(data, query, ctypes.byref(pointer),
                                      ctypes.byref(length)) \
                    and pointer.value and length.value > 1:
                text = ctypes.wstring_at(pointer, length.value - 1).strip()
                if text:
                    return text
    except Exception as exc:                  # noqa: BLE001 - 名前が取れなくても続ける
        log.debug("版情報を読めませんでした (%s): %s", path, exc)
    return ""


# ------------------------------------------------------------------
# プロセス
# ------------------------------------------------------------------
def _normalize(path: str) -> str:
    return (path or "").strip().strip('"').replace("\\", "/").rstrip("/").lower()


def _processes() -> list[tuple[int, int, str]]:
    """いま動いているプロセスの (PID, 親のPID, 実行ファイル名) の一覧。"""
    if IS_WINDOWS:
        return _processes_windows()
    found = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        name, _, rest = stat.rpartition(")")
        fields = rest.split()
        if len(fields) < 2 or fields[0] == "Z":
            continue                          # 終わって引き取りを待つだけのもの
        found.append((int(entry.name), int(fields[1]),
                      name.partition("(")[2]))
    return found


def _processes_windows() -> list[tuple[int, int, str]]:
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                             ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == wintypes.HANDLE(-1).value:
            return []
        found = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                found.append((int(entry.th32ProcessID),
                              int(entry.th32ParentProcessID), entry.szExeFile))
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return found
    except Exception as exc:                  # noqa: BLE001 - 一覧が取れなくても続ける
        log.debug("プロセスの一覧を取れませんでした: %s", exc)
        return []


# 親子の起動時刻の比べの余裕 (秒)。子が親より前に起動することはない
_PARENT_MARGIN_SEC = 0.05


def _older_than_parent(child: int, parent: int, starts: dict) -> bool:
    """子のはずのプロセスが、親より**前に**起動していたか。

    Windows の「親のPID」は、親が終わったあとも番号のまま残る。番号は
    すぐ使い回されるので、**同じ番号の別のプロセスの子**に見えてしまう
    (Start.vbs の wscript が終わり、その番号でツールが起動すると、
    ランチャーがツールの子に見える)。起動時刻で見分ける。
    """
    for pid in (child, parent):
        if pid not in starts:
            starts[pid] = process_started_at(pid)
    child_at, parent_at = starts[child], starts[parent]
    if child_at is None or parent_at is None:
        return False                          # 分からなければ親子とみる
    return child_at + _PARENT_MARGIN_SEC < parent_at


def protected_pids(rows: Optional[list] = None) -> set[int]:
    """ランチャー自身と、その親・祖先 (起動に使った pyw.exe・wscript など)。

    **どの処理でもツールとして扱わない・止めない。** ツールのフォルダーに
    ランチャーのフォルダーが入っている置き方では、ランチャーを起こした
    pyw.exe のコマンドラインがそのフォルダーを指すので、ツールの一部に
    見えてしまう (その子のランチャーごと止めることになる)。
    """
    me = os.getpid()
    if rows is None:
        rows = _processes()
    parent_of = {pid: parent for pid, parent, _ in rows}
    starts: dict = {}
    result = {me}
    child = me
    while True:
        parent = parent_of.get(child)
        if not parent or parent in result or parent in (0, 4) or parent not in parent_of:
            break
        if _older_than_parent(child, parent, starts):
            break                             # 番号を使い回された別のプロセス
        result.add(parent)
        child = parent
    return result


def process_tree(pid: Pids) -> set[int]:
    """そのプロセス (いくつでも) と、その子・孫。**動いているものだけ。**

    窓は本体ではなく子プロセスが出すことがある (PyInstaller で1ファイルに
    まとめた exe は、本体が子を起こして、窓は子が出す)。前に出す・閉じる
    ときは子まで見る。起こした本人が先に終わっても、子は残る (親の番号を
    覚えたまま) ので、終わった本人の番号からでも子をたどれる。

    ただし**ランチャー自身とその祖先は決して入れない** (`protected_pids`)。
    また、親より前に起動した「子」は、番号を使い回された別のプロセスの
    子なので入れない (`_older_than_parent`)。
    """
    roots = {pid} if isinstance(pid, int) else set(pid)
    roots = {p for p in roots if p and p > 0}
    if not roots:
        return set()
    rows = _processes()
    protected = protected_pids(rows)
    # 祖先から下へたどると、ランチャー自身や関係ないものまで入る
    roots -= protected - {os.getpid()}
    alive = {child for child, _, _ in rows}
    children: dict[int, list[int]] = {}
    for child, parent, _ in rows:
        if child != parent:
            children.setdefault(parent, []).append(child)
    starts: dict = {}
    tree = set(roots)
    stack = list(roots)
    while stack:
        parent = stack.pop()
        for child in children.get(parent, []):
            if child in tree or child in protected:
                continue
            if parent in alive and _older_than_parent(child, parent, starts):
                log.debug("PID %s は PID %s の子ではありません (番号の使い回し)",
                          child, parent)
                continue
            tree.add(child)
            stack.append(child)
    return (tree & alive) - protected


def process_image(pid: int) -> str:
    """そのPIDの実行ファイルのフルパス。動いていない・取れなければ空。"""
    if pid <= 0:
        return ""
    if not IS_WINDOWS:
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return ""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                                ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION,
                                      False, pid)
        if not handle:
            return ""
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) \
                    or code.value != _STILL_ACTIVE:
                return ""                     # 終わっている
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer,
                                                       ctypes.byref(size)):
                return ""
            return buffer.value
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:                  # noqa: BLE001
        log.debug("PID %s の実行ファイルを取れませんでした: %s", pid, exc)
        return ""


def system_boot_time() -> Optional[float]:
    """この端末が起動した時刻 (エポック秒)。分からなければ None。

    前のランチャーが片付けずに終わっていたとき、**そのあとで端末が
    起動し直していれば**電源断・再起動のせい (ランチャーの不具合ではない)。
    """
    if IS_WINDOWS:
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            return time.time() - kernel32.GetTickCount64() / 1000.0
        except Exception:                     # noqa: BLE001
            return None
    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def process_started_at(pid: int) -> Optional[float]:
    """そのPIDのプロセスが**いつ起動したか** (エポック秒)。分からなければ None。

    PIDは使い回される。記録に残ったPIDのプロセスが、記録を書いたあとで
    起動していれば、**それは記録を書いた本人ではない** (番号が同じだけ)。
    外部コマンドを使わずに確かめられるので、コマンドラインが取れない
    端末でも使える。
    """
    if pid <= 0:
        return None
    if IS_WINDOWS:
        return _started_at_windows(pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8",
                                                   errors="replace")
        ticks = int(stat.rpartition(")")[2].split()[19])
        boot = None
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                boot = int(line.split()[1])
                break
        if boot is None:
            return None
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def _started_at_windows(pid: int) -> Optional[float]:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
            ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION,
                                      False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            # FILETIME は 1601/1/1 からの 100ns 単位
            return (created - 116444736000000000) / 10_000_000
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:                  # noqa: BLE001
        log.debug("PID %s の起動時刻を取れませんでした: %s", pid, exc)
        return None


def command_line(pid: int) -> str:
    """そのPIDのコマンドライン。取れなければ空。

    Windows では `NtQueryInformationProcess` で読む (Windows 8.1 以降)。
    wmic・PowerShell を使わないので、それらを禁じている端末でも取れる。
    """
    if pid <= 0:
        return ""
    if not IS_WINDOWS:
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return ""
        return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    try:
        import ctypes
        from ctypes import wintypes

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [("Length", wintypes.USHORT),
                        ("MaximumLength", wintypes.USHORT),
                        ("Buffer", ctypes.c_void_p)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtQueryInformationProcess.argtypes = [
            wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG)]
        ntdll.NtQueryInformationProcess.restype = ctypes.c_long

        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION,
                                      False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.ULONG(0)
            ntdll.NtQueryInformationProcess(
                handle, _PROCESS_COMMAND_LINE_INFORMATION, None, 0,
                ctypes.byref(size))
            length = max(size.value, ctypes.sizeof(UNICODE_STRING) + 2)
            buffer = ctypes.create_string_buffer(length)
            status = ntdll.NtQueryInformationProcess(
                handle, _PROCESS_COMMAND_LINE_INFORMATION, buffer, length,
                ctypes.byref(size))
            if status != 0:
                return ""
            text = UNICODE_STRING.from_buffer(buffer)
            if not text.Buffer or not text.Length:
                return ""
            return ctypes.wstring_at(text.Buffer, text.Length // 2)
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:                  # noqa: BLE001
        log.debug("PID %s のコマンドラインを取れませんでした: %s", pid, exc)
        return ""


def folder_is_specific(folder: str) -> bool:
    """「このフォルダーから起動したプロセス = そのツール」と言える場所か。

    ドライブの直下・Windows・Program Files・利用者フォルダーの直下
    (デスクトップ・ドキュメントなど) は、ほかのアプリも起動する場所なので
    使わない。ここを緩めると、**無関係なプロセスをツールとして扱い、止めて
    しまう**ことになる。
    """
    text = _normalize(folder)
    if not text:
        return False
    parts = [part for part in text.split("/") if part]
    if len(parts) < 3:
        return False
    home = os.path.expanduser("~")
    broad = {os.environ.get(name, "") for name in (
        "WINDIR", "SYSTEMROOT", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "PROGRAMW6432", "PROGRAMDATA", "LOCALAPPDATA", "APPDATA", "USERPROFILE",
        "PUBLIC", "TEMP", "TMP")}
    broad |= {home} | {os.path.join(home, name) for name in (
        "Desktop", "Documents", "Downloads", "OneDrive")}
    broad |= {"/", "/usr", "/usr/bin", "/usr/local", "/usr/local/bin", "/opt",
              "/tmp", "/bin"}
    return text not in {_normalize(b) for b in broad if b}


# コマンドラインでフォルダーのものと見てよい「スクリプトを動かすだけの」
# 実行ファイル。**これ以外はコマンドラインでは見ない** ── メモ帳で
# ツールの app.json を開いているだけのものを、ツールとして止めないため
_SCRIPT_HOSTS = ("python", "pythonw", "py", "pyw", "cmd", "wscript", "cscript",
                 "node", "java", "javaw")


def _is_script_host(image: str) -> bool:
    # 区切りは「\」も「/」も見る (どちらの書き方で渡されても名前を取る)
    name = image.replace("\\", "/").rsplit("/", 1)[-1].lower()
    stem = name[:-4] if name.endswith(".exe") else name
    return stem.rstrip("0123456789.") in _SCRIPT_HOSTS


def processes_in_folder(folder: str) -> list[int]:
    """**そのフォルダーから起動した**プロセス。

    * 実行ファイルがフォルダーの中 (ツールの exe・同梱の Python)
    * スクリプトを動かす実行ファイル (python・cmd・wscript など) で、
      コマンドラインがフォルダーの中のものを指している

    フォルダーが広すぎるとき (`folder_is_specific`) は何も返さない。
    """
    if not folder_is_specific(folder):
        return []
    wanted = _normalize(folder) + "/"
    rows = _processes()
    protected = protected_pids(rows)
    found = []
    for pid, _, _ in rows:
        if pid in (0, 4) or pid in protected:
            continue
        image = process_image(pid)
        if IS_WINDOWS and _normalize(image).startswith(wanted):
            found.append(pid)
            continue
        if image and not _is_script_host(image):
            continue
        if wanted in _normalize(command_line(pid)) + "/":
            if not IS_WINDOWS and _is_zombie(pid):
                continue
            found.append(pid)
    return found


def related_pids(folder: str = "", roots: Pids = ()) -> set[int]:
    """「そのツール」のプロセス一式。

    ツールのフォルダーから起動したプロセスと、ランチャーが起こしたプロセス
    (`roots`)、それぞれの子・孫。**起こした exe がもう終わっていても**、
    本体が残っていれば見つかる。
    """
    base = set(processes_in_folder(folder)) if folder else set()
    base |= {roots} if isinstance(roots, int) else set(roots)
    return process_tree(base)


def _is_zombie(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8",
                                                   errors="replace")
    except OSError:
        return True
    return stat.rpartition(")")[2].split()[:1] == ["Z"]


def listening_ports(pids: Pids) -> list[tuple[str, int]]:
    """それらのプロセスが待ち受けているポート。`(ホスト, ポート)` の一覧。

    設定のポートで答えないとき、**本当はどこで待ち受けているか**を探す。
    Windows は `GetExtendedTcpTable` (ctypes)、ほかは `/proc/net/tcp`。
    """
    wanted = {pids} if isinstance(pids, int) else set(pids)
    if not wanted:
        return []
    try:
        rows = _listeners_windows() if IS_WINDOWS else _listeners_proc()
    except Exception as exc:                  # noqa: BLE001
        log.debug("待ち受けているポートを取れませんでした: %s", exc)
        return []
    found = []
    for host, port, pid in rows:
        if pid in wanted and (host, port) not in found:
            found.append((host, port))
    return sorted(found, key=lambda item: (item[1], item[0]))


def port_listening(port: int) -> Optional[bool]:
    """そのポートで**誰かが待ち受けているか**。分からなければ None。

    接続して確かめると、Windows では閉じたポートに断られるまで 1〜2 秒
    かかる。待ち受けの一覧を見れば一瞬で分かる。
    """
    if port <= 0:
        return False
    try:
        rows = _listeners_windows() if IS_WINDOWS else _listeners_proc()
    except Exception:                         # noqa: BLE001
        return None
    return any(p == port for _, p, _ in rows)


def _listeners_windows() -> list[tuple[str, int, int]]:
    import ctypes
    from ctypes import wintypes

    iphlpapi = ctypes.WinDLL("iphlpapi")
    iphlpapi.GetExtendedTcpTable.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD

    class ROW4(ctypes.Structure):
        _fields_ = [("state", wintypes.DWORD), ("local_addr", wintypes.DWORD),
                    ("local_port", wintypes.DWORD), ("remote_addr", wintypes.DWORD),
                    ("remote_port", wintypes.DWORD), ("pid", wintypes.DWORD)]

    class ROW6(ctypes.Structure):
        _fields_ = [("local_addr", ctypes.c_ubyte * 16),
                    ("local_scope", wintypes.DWORD),
                    ("local_port", wintypes.DWORD),
                    ("remote_addr", ctypes.c_ubyte * 16),
                    ("remote_scope", wintypes.DWORD),
                    ("remote_port", wintypes.DWORD),
                    ("state", wintypes.DWORD), ("pid", wintypes.DWORD)]

    rows: list[tuple[str, int, int]] = []
    for family, row_type in ((socket.AF_INET, ROW4), (_AF_INET6, ROW6)):
        size = wintypes.DWORD(0)
        iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, family,
                                     _TCP_TABLE_OWNER_PID_LISTENER, 0)
        if not size.value:
            continue
        buffer = ctypes.create_string_buffer(size.value + 1024)
        size = wintypes.DWORD(len(buffer))
        if iphlpapi.GetExtendedTcpTable(buffer, ctypes.byref(size), False,
                                        family, _TCP_TABLE_OWNER_PID_LISTENER, 0):
            continue
        count = wintypes.DWORD.from_buffer(buffer).value
        # 件数 (DWORD) のすぐ後ろに行が並ぶ (どちらの表も 4 バイト境界)
        offset = ctypes.sizeof(wintypes.DWORD)
        array = (row_type * count).from_buffer(buffer, offset)
        for row in array:
            port = socket.ntohs(row.local_port & 0xFFFF)
            if family == socket.AF_INET:
                host = socket.inet_ntoa(row.local_addr.to_bytes(4, "little"))
            else:
                host = socket.inet_ntop(socket.AF_INET6, bytes(row.local_addr))
            rows.append((host, port, int(row.pid)))
    return rows


def _listeners_proc() -> list[tuple[str, int, int]]:
    """`/proc/net/tcp` の LISTEN と、その持ち主のPID。"""
    inodes: dict[str, tuple[str, int]] = {}
    for name, six in (("tcp", False), ("tcp6", True)):
        try:
            lines = Path(f"/proc/net/{name}").read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":     # 0A = LISTEN
                continue
            address, port = fields[1].split(":")
            host = "::" if six else socket.inet_ntoa(
                int(address, 16).to_bytes(4, "little"))
            inodes[fields[9]] = (host, int(port, 16))
    rows = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(fd)
            except OSError:
                continue
            if link.startswith("socket:[") and link[8:-1] in inodes:
                host, port = inodes[link[8:-1]]
                rows.append((host, port, int(entry.name)))
    return rows


def runs_exe(pid: int, exe_path: str) -> bool:
    """そのPIDが**その exe を実行しているか**。動いていなければ偽。

    PIDは使い回される。記録したPIDが、いまは別のプロセスになっている
    ことがあるので、止める・前に出す・「動いている」と答える前に確かめる。
    Windows では実行ファイルのフルパスで、ほかでは `/proc` のコマンド
    ラインで照合する (試験ではスクリプトを exe に見立てるため)。
    """
    wanted = _normalize(exe_path)
    if pid <= 0 or not wanted:
        return False
    if IS_WINDOWS:
        image = process_image(pid)
        if not image:
            return False
        if _normalize(image) == wanted:
            return True
        # 書き方が違うだけで同じファイルのことがある (ネットワークドライブの
        # Z:\ と \\server\share、短い名前 PROGRA~1 など)。**ファイルそのもの**
        # で比べる
        try:
            return os.path.samefile(image, exe_path.strip().strip('"'))
        except OSError:
            return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8",
                                                   errors="replace")
        if stat.rpartition(")")[2].split()[:1] == ["Z"]:
            return False
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return wanted in _normalize(raw.replace(b"\0", b" ").decode("utf-8", "replace"))


def find_by_exe(exe_path: str) -> list[int]:
    """その exe を実行しているプロセス (いちばん上の親だけ)。

    ランチャーの外 (デスクトップのショートカットなど) で起動されていた
    アプリを見つけ、**もう1つ起動しない**ために使う。同じ exe が親子で
    動いている (1ファイルにまとめた exe) ときは、親だけを返す。
    """
    wanted = _normalize(exe_path)
    if not wanted:
        return []
    name = wanted.rsplit("/", 1)[-1]
    rows = _processes()
    if IS_WINDOWS:
        candidates = [pid for pid, _, exe in rows if exe.lower() == name]
    else:
        candidates = [pid for pid, _, _ in rows]
    matched = {pid for pid in candidates if runs_exe(pid, exe_path)}
    parents = {pid: parent for pid, parent, _ in rows}
    return sorted(pid for pid in matched if parents.get(pid) not in matched)


# ------------------------------------------------------------------
# 窓
# ------------------------------------------------------------------
def _windows_of(pids: set[int]) -> Optional[list[int]]:
    """それらのプロセスが持つ、見えている最上位の窓。Windows 以外は None。"""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                           wintypes.LPARAM)
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                    ctypes.POINTER(wintypes.DWORD)]

        found: list[int] = []

        def visit(hwnd, _param):
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindow(hwnd, _GW_OWNER):
                return True                   # ダイアログなど、持ち主のいる窓
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value in pids:
                found.append(hwnd)
            return True

        user32.EnumWindows(callback_type(visit), 0)
        return found
    except Exception as exc:                  # noqa: BLE001 - 窓が分からなくても続ける
        log.debug("窓の一覧を取れませんでした: %s", exc)
        return None


def has_window(pid: Pids) -> Optional[bool]:
    """そのアプリ (子も含む) の窓が出ているか。分からなければ None。"""
    windows = _windows_of(process_tree(pid))
    return None if windows is None else bool(windows)


def bring_to_front(pid: Pids) -> bool:
    """そのアプリの窓を前に出す。出せたら True。

    最小化されていれば戻す。押したのはランチャーのボタンなので、
    前に出すことを Windows が許す (ランチャーが手前にいる)。
    """
    windows = _windows_of(process_tree(pid))
    return bool(windows) and activate(windows[0])


def activate(hwnd: int) -> bool:
    """その窓を前に出す (最小化されていれば戻す)。"""
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, _SW_RESTORE)
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception as exc:                  # noqa: BLE001
        log.debug("窓を前に出せませんでした (hwnd=%s): %s", hwnd, exc)
        return False


def windows_by_title(text: str) -> Optional[list[tuple[int, str]]]:
    """題名に `text` を含む、見えている最上位の窓 `(窓, 題名)`。Windows 以外は None。

    ツールの画面が**ふだんのブラウザーの窓**として開かれていると、その窓の
    持ち主はブラウザー本体で、ツールのプロセスからはたどれない。前に出す
    ときだけ、題名で探す (閉じるときには使わない ── 題名が同じ別の窓を
    閉じかねない)。
    """
    wanted = (text or "").strip()
    if not IS_WINDOWS or not wanted:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                           wintypes.LPARAM)
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR,
                                          ctypes.c_int]
        found: list[tuple[int, str]] = []

        def visit(hwnd, _param):
            if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, _GW_OWNER):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if wanted in buffer.value:
                found.append((hwnd, buffer.value))
            return True

        user32.EnumWindows(callback_type(visit), 0)
        return found
    except Exception as exc:                  # noqa: BLE001
        log.debug("窓を題名で探せませんでした: %s", exc)
        return None


def close_windows(pid: Pids) -> Optional[int]:
    """そのアプリの窓に「閉じて」と頼む (×ボタンと同じ)。頼んだ窓の数。

    **落とすのではなく頼む。** アプリは保存の確認を出したり、後片付けを
    してから終われる。窓が出ていなければ 0、Windows 以外は None。
    """
    windows = _windows_of(process_tree(pid))
    if windows is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        sent = 0
        for hwnd in windows:
            if user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0):
                sent += 1
        return sent
    except Exception as exc:                  # noqa: BLE001
        log.debug("窓を閉じられませんでした (pid=%s): %s", pid, exc)
        return 0
