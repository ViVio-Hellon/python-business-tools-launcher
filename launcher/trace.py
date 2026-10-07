"""後追いの記録 (なぜなぜ分析のため)

「起動しなかった」「急に止まった」を、**あとから順を追って調べられる**
ようにする。ふつうのログ (`launcher_*.log`) は文章で、何が起きたかを
探すのに読み込みが要る。そこで次の2つを別に残す。

    出来事の一覧  events_YYYYMM.csv
        1行1件。Excel で開いて、日時・ツール・種類・結果で絞り込める。
        **操作ID**で、1回の操作 (ボタンを押した〜起動できなかった) の
        行をまとめて追える。

    障害記録      incidents/YYYYMMDD_HHMMSS_<ツール>_<現象>.txt
        失敗・思わぬ停止・想定外の例外が起きたときに1件。なぜなぜ分析の
        書式で、**ランチャーが確かめられたところまで「なぜ」を埋めて**
        おく。その先は人が書き足す。直前の操作の流れ、ランチャーが見た
        こと (戻り値・ポート・応答)、ツールの出力の最後の行も付ける。

【置き場所】
［設定］の「ログの出力先」→ 配布先フォルダ → 既定 (`%LOCALAPPDATA%\\...\\logs`)
の順に決める。指定した場所には **端末名のフォルダー**を作って書く ──
共有フォルダーを指定すれば、全端末の記録が1か所に集まる。

**書けなければ既定の場所へ退く。** 記録のためにランチャーが止まるほうが
困る。退いたこと自体も記録する。

ツールの出力 (`tool_<アプリID>.out.log`) だけは、いつも端末の中に置く。
ツールが書き続けるファイルなので、共有フォルダーにするとネットワークが
切れたときにツールの書き込みが失敗し、ツールごと止まりかねない。
障害記録にはその末尾を写しておく。
"""
from __future__ import annotations

import csv
import getpass
import os
import platform
import re
import secrets
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import app_config
from .logging_utils import get_logger

log = get_logger("trace")

# ［設定］の「ログの出力先」 (端末ごとの設定DBの鍵)
LOG_DIR_KEY = "log_dir"

EVENT_FIELDS = ("日時", "端末", "利用者", "ランチャー版", "操作ID", "種類",
                "結果", "ツール", "アプリID", "ツール版", "ポート", "PID",
                "経過秒", "原因", "詳細", "障害記録")

# 結果の呼び名。Excel で絞り込むときの言葉
OK = "成功"
FAILED = "失敗"
WARNING = "注意"
INFO = "情報"
CANCELLED = "中止"

# 障害記録とツール出力の写しの長さ
TAIL_LINES = 40

# 残す長さ。**自分の端末のフォルダーの中だけ**片付ける (共有フォルダーの
# ほかの端末の記録には触らない)
INCIDENT_KEEP_DAYS = 365
EVENT_KEEP_DAYS = 400

_lock = threading.RLock()
_destination: Optional["Destination"] = None
_warned_fallback = False
# 想定外の例外のうち、障害記録を書いたもの。**同じ失敗で記録を量産しない**
# (見回りのように数秒ごとに回る処理で起きると、1日で数千件になる)
_seen_unexpected: set[tuple[str, str, str]] = set()


# ------------------------------------------------------------------
# 置き場所
# ------------------------------------------------------------------
@dataclass(frozen=True)
class Destination:
    path: Path                # 実際に書く場所
    configured: str = ""      # 指定された場所 (空なら既定)
    source: str = "既定"      # どこで指定されたか
    problem: str = ""         # 指定先に書けず、既定へ退いた理由

    def describe(self) -> str:
        # ほかのアプリから見た場所で出す (`real_path`)
        shown = real_path(self.path)
        if not self.configured:
            return f"{shown} (既定)"
        if self.problem:
            return (f"{shown} (指定先 {self.configured} に書けないため退避中:"
                    f" {self.problem})")
        return f"{shown} ({self.source}で指定)"


def local_dir() -> Path:
    return app_config.local_dir("logs")


def computer_name() -> str:
    name = os.environ.get("COMPUTERNAME") or platform.node() or "unknown"
    return _safe(name) or "unknown"


def user_name() -> str:
    try:
        return getpass.getuser()
    except Exception:                         # noqa: BLE001 - 名前が取れなくても続ける
        return ""


def configured_dir() -> tuple[str, str]:
    """指定された出力先と、その出どころ。指定が無ければ `("", "既定")`。

    その端末の［設定］ → 配布先フォルダ の順。**その端末で決めたものが優先。**
    """
    from . import distribution, tool_registry

    value = tool_registry.peek_pc_setting(LOG_DIR_KEY).strip()
    if value:
        return value, "この端末の設定"
    value = distribution.log_dir().strip()
    if value:
        return value, "配布先フォルダ"
    return "", "既定"


def expand_dir(text: str) -> Path:
    """`%USERPROFILE%` などを展開し、相対ならランチャーのフォルダーから。"""
    raw = os.path.expandvars((text or "").strip().strip('"'))
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = app_config.APP_ROOT / path
    return path


def folder_for(text: str) -> Path:
    """指定された出力先に、実際に書く場所 (端末名のフォルダー)。"""
    return expand_dir(text) / computer_name()


def check_writable(folder: Path) -> str:
    """書けるか。書けなければ理由。"""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".write-test-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return str(exc)
    return ""


def destination(*, refresh: bool = False) -> Destination:
    """いま記録を書く場所。決まったら覚えておく (`refresh` で決め直す)。"""
    global _destination
    with _lock:
        if _destination is not None and not refresh:
            return _destination
        local = local_dir()
        try:
            text, source = configured_dir()
        except Exception as exc:              # noqa: BLE001 - 記録の都合で止めない
            log.warning("ログの出力先を読めませんでした: %s", exc)
            text, source = "", "既定"
        if not text:
            _destination = Destination(local)
        else:
            target = folder_for(text)
            problem = check_writable(target)
            if problem:
                log.warning("ログの出力先に書けないので既定の場所へ退きます: "
                            "%s (%s)", target, problem)
                _destination = Destination(local, text, source, problem)
            else:
                _destination = Destination(target, text, source)
        return _destination


def set_log_dir(text: str) -> Destination:
    """［設定］の「ログの出力先」を保存し、**書き先をその場で移す**。

    空なら既定に戻す。変えたことは、**移す前と後の両方**に残す ──
    どちらの記録を見ても、続きがどこにあるか分かるように。
    """
    from . import logging_utils, tool_registry

    text = (text or "").strip()
    before = destination()
    event("ログの出力先を変更", INFO,
          detail=f"{before.path} → {folder_for(text) if text else local_dir()}")
    if text:
        tool_registry.set_pc_setting(LOG_DIR_KEY, text)
    else:
        tool_registry.clear_pc_setting(LOG_DIR_KEY)
    after = destination(refresh=True)
    logging_utils.reconfigure()
    event("ログの出力先を変更", WARNING if after.problem else OK,
          cause=after.problem, detail=f"{before.path} → {after.path}")
    return after


# ------------------------------------------------------------------
# 操作 (1回のボタン操作の流れ)
# ------------------------------------------------------------------
@dataclass
class Operation:
    """1回の操作。**その操作で起きたことを順に覚えておく。**

    障害記録の「直前の操作の流れ」はここから書く。出来事の一覧にも
    同じ操作IDが入るので、Excel で操作IDを絞り込めば同じ流れが追える。
    """

    op_id: str
    kind: str
    tool_name: str = ""
    started: float = field(default_factory=time.time)
    trail: list[tuple[float, str]] = field(default_factory=list)

    def step(self, text: str) -> None:
        with _lock:
            self.trail.append((time.time(), text))
        log.debug("[%s] %s", self.op_id, text)


def operation(kind: str, tool: Any = None) -> Operation:
    op_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    return Operation(op_id=op_id, kind=kind, tool_name=_name_of(tool))


# ------------------------------------------------------------------
# 出来事の一覧
# ------------------------------------------------------------------
def event(kind: str, result: str = INFO, *, tool: Any = None,
          op: Optional[Operation] = None, cause: str = "", detail: str = "",
          version: str = "", port: int = 0, pid: int = 0,
          elapsed: Optional[float] = None, incident: str = "") -> None:
    """出来事を1行残す。**失敗しても例外は出さない** (記録で止めない)。"""
    try:
        row = {
            "日時": time.strftime("%Y/%m/%d %H:%M:%S"),
            "端末": computer_name(),
            "利用者": user_name(),
            "ランチャー版": app_config.version(),
            "操作ID": op.op_id if op else "",
            "種類": kind,
            "結果": result,
            "ツール": _name_of(tool),
            "アプリID": getattr(tool, "app_id", "") if tool is not None else "",
            "ツール版": version,
            "ポート": port or getattr(tool, "port", 0) or "",
            "PID": pid or getattr(tool, "pid", 0) or "",
            "経過秒": "" if elapsed is None else f"{elapsed:.1f}",
            "原因": cause,
            "詳細": " / ".join(line for line in detail.splitlines() if line.strip()),
            "障害記録": incident,
        }
        if op is not None:
            op.step(kind + ("" if result == INFO else f" ({result})")
                    + (f": {cause}" if cause else ""))
        level = log.warning if result in (FAILED, WARNING) else log.info
        level("[出来事] %s %s %s%s", kind, result, row["ツール"],
              f" — {cause}" if cause else "")
        _append_row(row)
    except Exception:                         # noqa: BLE001 - 記録で止めない
        log.exception("出来事を記録できませんでした: %s", kind)


def events_path(dest: Optional[Path] = None) -> Path:
    return (dest or destination().path) / f"events_{time.strftime('%Y%m')}.csv"


def _append_row(row: dict) -> None:
    """CSVへ1行足す。**Excel で開いたままだと書けない**ので、そのときは
    端末の中の同じ名前のファイルへ書く (記録を失わない)。"""
    global _warned_fallback
    with _lock:
        target = events_path()
        try:
            _write_row(target, row)
            return
        except OSError as exc:
            fallback = events_path(local_dir())
            if fallback == target:
                raise
            if not _warned_fallback:
                log.warning("出来事の一覧に書けないので端末の中へ書きます "
                            "(Excel で開いていませんか): %s (%s)", target, exc)
                _warned_fallback = True
            _write_row(fallback, row)


def _write_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    # 新しく作るときだけ BOM を付ける。**Excel が UTF-8 と分かるように**
    # (付けないと文字化けする)
    with open(path, "a", encoding="utf-8-sig" if new else "utf-8",
              newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EVENT_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def read_events(path: Optional[Path] = None) -> list[dict]:
    """出来事の一覧を読む (試験・診断用)。"""
    target = path or events_path()
    try:
        with open(target, encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


# ------------------------------------------------------------------
# 障害記録
# ------------------------------------------------------------------
def incident(title: str, *, tool: Any = None, op: Optional[Operation] = None,
             whys: Iterable[str] = (), observed: Iterable[tuple[str, str]] = (),
             hints: Iterable[str] = (), tool_log: Optional[Path] = None,
             exc: Optional[BaseException] = None) -> str:
    """障害記録を1件書き、その場所を返す。書けなければ空。

    `whys` は**ランチャーが確かめられた「なぜ」**だけを入れる。推測は
    `hints` (次に確かめること) へ分けて書く ── なぜなぜ分析で、確かめて
    いないことが事実のように並ぶと、誤った対策に向かう。その先は人が
    書き足す。
    """
    try:
        text = render_incident(title, tool=tool, op=op, whys=list(whys),
                               observed=list(observed), hints=list(hints),
                               tool_log=tool_log, exc=exc)
        name = (f"{time.strftime('%Y%m%d_%H%M%S')}_"
                f"{_safe(_name_of(tool)) or 'ランチャー'}_{_safe(title)[:30]}.txt")
        path = _write_incident(name, text)
        log.warning("障害記録を残しました: %s", path)
        return str(path)
    except Exception:                         # noqa: BLE001 - 記録で止めない
        log.exception("障害記録を書けませんでした: %s", title)
        return ""


def _write_incident(name: str, text: str) -> Path:
    # メモ帳で開いて読めるよう、BOM 付き UTF-8・CRLF にする
    data = text.replace("\r\n", "\n").replace("\n", "\r\n")
    for folder in (destination().path / "incidents", local_dir() / "incidents"):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = _unused_name(folder / name)
            # `write_text(newline=...)` は Python 3.10 から。バイトで書く
            path.write_bytes(data.encode("utf-8-sig"))
            return path
        except OSError as exc:
            log.warning("障害記録を書けませんでした (%s): %s", folder, exc)
    raise OSError("障害記録をどこにも書けませんでした")


def _unused_name(path: Path) -> Path:
    """同じ秒・同じ題名の記録が重なっても**前のものを上書きしない**。"""
    candidate = path
    number = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
        number += 1
    return candidate


def render_incident(title: str, *, tool: Any = None,
                    op: Optional[Operation] = None, whys: list[str],
                    observed: list[tuple[str, str]],
                    hints: Optional[list[str]] = None,
                    tool_log: Optional[Path] = None,
                    exc: Optional[BaseException] = None) -> str:
    """障害記録の本文 (なぜなぜ分析の書式)。"""
    lines: list[str] = []
    add = lines.append
    add(f"障害記録  {time.strftime('%Y/%m/%d %H:%M:%S')}")
    add("=" * 64)
    add(f"現象      : {title}")
    add(f"ツール    : {_name_of(tool) or '(ランチャー自身)'}"
        + (f"  ({tool.app_id})" if getattr(tool, "app_id", "") else ""))
    add(f"端末      : {computer_name()}  (利用者 {user_name() or '?'})")
    add(f"ランチャー: {app_config.version_label()} / Python "
        f"{sys.version.split()[0]}")
    if op is not None:
        add(f"操作ID    : {op.op_id}  (出来事の一覧をこのIDで絞り込むと同じ流れが見えます)")
    add("")

    add("■ なぜなぜ")
    add("  ランチャーが確かめられたところまで書いてあります。")
    add("  その先 (なぜそうなったか) を書き足し、根本の原因と対策まで進めてください。")
    add("")
    add(f"  現象   {title}")
    for index, why in enumerate(whys, start=1):
        first, *rest = (why or "").splitlines() or [""]
        add(f"  なぜ{index}  {first}")
        for extra in rest:
            add(f"         {extra}")
    for index in range(len(whys) + 1, max(len(whys) + 2, 6)):
        add(f"  なぜ{index}  ")
    add("  根本原因 ")
    add("  対策     ")
    add("")

    if hints:
        add("■ 次に確かめること (ランチャーの見立て。確かめてはいません)")
        for hint in hints:
            first, *rest = hint.splitlines() or [""]
            add(f"  ・{first}")
            for extra in rest:
                add(f"    {extra}")
        add("")

    if op is not None and op.trail:
        add("■ 直前の操作の流れ")
        for at, text in op.trail:
            add(f"  {time.strftime('%H:%M:%S', time.localtime(at))}  {text}")
        add("")

    if observed:
        add("■ ランチャーが見たこと")
        width = max(_width(key) for key, _ in observed)
        for key, value in observed:
            pad = " " * (width - _width(key))
            first, *rest = (str(value) or "-").splitlines() or ["-"]
            add(f"  {key}{pad} : {first}")
            for extra in rest:
                add(f"  {' ' * width}   {extra}")
        add("")

    if exc is not None:
        add("■ 想定外の例外")
        for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
            for part in line.rstrip().splitlines():
                add(f"  {part}")
        add("")

    if tool_log is not None:
        tail = tail_lines(tool_log, TAIL_LINES)
        add(f"■ ツールの出力 (最後の{TAIL_LINES}行)  {tool_log}")
        if tail:
            for line in tail:
                add(f"  | {line}")
        else:
            add("  (出力はありません)")
        add("")

    add("■ 関連する記録")
    from .logging_utils import log_file_path
    add(f"  ランチャーのログ : {log_file_path()}")
    add(f"  出来事の一覧     : {events_path()}")
    if tool_log is not None:
        add(f"  ツールの出力     : {tool_log}")
    return "\n".join(lines) + "\n"


def unexpected(where: str, exc: BaseException, *, tool: Any = None,
               op: Optional[Operation] = None) -> str:
    """想定外の例外を障害記録にする。**同じ失敗は1回だけ**書く。

    書いたときはその場所、書かなかった (2回目以降) ときは空を返す。
    """
    key = (where, type(exc).__name__, str(exc))
    with _lock:
        if key in _seen_unexpected:
            log.debug("同じ失敗の障害記録はもう書いてあります: %s", key)
            return ""
        _seen_unexpected.add(key)
    cause = f"{type(exc).__name__}: {exc}"
    path = incident(
        f"{where}で想定外の失敗", tool=tool, op=op,
        whys=[f"ランチャーの中で想定外の例外が起きた ({cause})"],
        hints=["ランチャーの不具合の可能性があります。"
               "この記録を開発担当へ渡してください。"],
        exc=exc)
    event("想定外の例外", FAILED, tool=tool, op=op, cause=cause, detail=where,
          incident=path)
    return path


def install_thread_hook() -> None:
    """裏の処理 (スレッド) で拾われなかった例外も障害記録にする。

    裏の処理が落ちると、画面には何も出ず「押しても反応しない」としか
    見えない。いちばん後追いしにくい失敗なので、ここで拾う。
    """
    previous = threading.excepthook

    def hook(args: "threading.ExceptHookArgs") -> None:
        if args.exc_value is not None and not isinstance(args.exc_value,
                                                         SystemExit):
            name = args.thread.name if args.thread is not None else "?"
            try:
                unexpected(f"裏の処理 ({name})", args.exc_value)
            except Exception:                 # noqa: BLE001 - 記録で止めない
                pass
        previous(args)

    threading.excepthook = hook


def real_path(path: "str | Path") -> str:
    """ほかのアプリ (メモ帳・エクスプローラ) から見た、その場所。

    **Microsoft Store 版の Python** は、AppData に書いたファイルを、その
    Python だけに見える別の場所 (`AppData\\Local\\Packages\\Python...\\
    LocalCache\\...`) へ振り替える。ランチャーには `AppData\\Local\\
    BusinessToolsLauncher` に見えても、メモ帳やエクスプローラには
    「パスが見つかりません」になる。実体の場所をたどって返す。
    """
    try:
        return os.path.realpath(str(path))
    except (OSError, ValueError):
        return str(path)


def is_store_python() -> bool:
    """Microsoft Store 版の Python で動いているか (AppData が振り替えられる)。"""
    text = f"{sys.executable}|{sys.base_prefix}".lower()
    return "windowsapps" in text or "pythonsoftwarefoundation" in text


def moved_by_python(path: "str | Path") -> bool:
    """ランチャーに見える場所と、ほかのアプリに見える場所が違うか。"""
    text = str(path or "")
    if not text:
        return False
    return os.path.normcase(os.path.abspath(text)) != os.path.normcase(real_path(text))


# 画面に写す記録の長さの上限 (文字)。障害記録はふつう数千文字
READ_LIMIT = 200_000


def read_record(path: "str | Path") -> tuple[str, str]:
    """記録の中身を**ランチャー自身が**読む。戻り値は (中身, 読めなかった理由)。

    ランチャーが書いたファイルは、ランチャーからは必ず見える (Microsoft
    Store 版の Python が場所を振り替えていても)。メモ帳で開けない端末でも
    ［詳細］で読めるように、ここで読んで画面に写す。
    """
    try:
        with open(str(path), "rb") as handle:
            data = handle.read(READ_LIMIT * 4 + 1)
    except OSError as exc:
        return "", f"{exc.strerror or exc}"
    text = data.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    if len(text) > READ_LIMIT or len(data) > READ_LIMIT * 4:
        text = text[:READ_LIMIT] + "\n…(長いので途中まで)"
    return text, ""


def open_path(path: "str | Path") -> bool:
    """記録をふだんのアプリ (メモ帳・Excel・エクスプローラ) で開く。

    開けなければ偽 (理由はログ)。**実体の場所** (`real_path`) を渡す。
    """
    import subprocess

    target = real_path(path)
    if not os.path.exists(target):
        log.warning("開けませんでした (見つかりません): %s → %s", path, target)
        return False
    try:
        if os.name == "nt":
            try:
                os.startfile(target)          # type: ignore[attr-defined]
                return True
            except OSError as exc:
                log.warning("関連づけで開けませんでした (%s): %s", target, exc)
            program = "explorer.exe" if os.path.isdir(target) else "notepad.exe"
            subprocess.Popen([program, target])
            return True
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, target], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except (OSError, AttributeError) as exc:
        log.warning("開けませんでした (%s): %s", target, exc)
        return False


def reveal_path(path: "str | Path") -> bool:
    """エクスプローラでそのファイルを選んだ状態で開く (フォルダーなら開く)。"""
    import subprocess

    target = real_path(path)
    if os.name != "nt":
        return open_path(os.path.dirname(target) if os.path.isfile(target) else target)
    try:
        if os.path.isfile(target):
            subprocess.Popen(["explorer.exe", f"/select,{target}"])
        else:
            subprocess.Popen(["explorer.exe", target])
        return True
    except OSError as exc:
        log.warning("エクスプローラで開けませんでした (%s): %s", target, exc)
        return False


def recent_incidents(days: int = 7) -> list[Path]:
    """最近の障害記録 (新しい順)。"""
    limit = time.time() - days * 86400
    found: list[Path] = []
    for folder in {destination().path / "incidents", local_dir() / "incidents"}:
        try:
            found.extend(p for p in folder.glob("*.txt")
                         if p.stat().st_mtime >= limit)
        except OSError:
            continue
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


# ------------------------------------------------------------------
# 手がかりを集める
# ------------------------------------------------------------------
def tail_lines(path: Optional[Path], count: int = TAIL_LINES) -> list[str]:
    """ファイルの最後の `count` 行。読めなければ空。"""
    if path is None:
        return []
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 64 * 1024))
            raw = handle.read()
    except OSError:
        return []
    for encoding in ("utf-8", "cp932"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    lines = [line.rstrip() for line in text.splitlines()]
    return [line for line in lines if line][-count:]


# ツールの出力のうち、エラーらしい行の印
_ERROR_MARK = re.compile(r"(Error|Exception|Traceback|エラー|失敗|見つかりません)")


def error_line(lines: list[str]) -> str:
    """エラーらしい最後の行。無ければ空。"""
    for line in reversed(lines):
        if _ERROR_MARK.search(line):
            return line.strip()
    return ""


# ------------------------------------------------------------------
# 片付け
# ------------------------------------------------------------------
def purge_old() -> None:
    """古い記録を片付ける。**自分の端末のフォルダーの中だけ。**"""
    now = time.time()
    for folder, pattern, days in (
            (destination().path / "incidents", "*.txt", INCIDENT_KEEP_DAYS),
            (local_dir() / "incidents", "*.txt", INCIDENT_KEEP_DAYS),
            (destination().path, "events_*.csv", EVENT_KEEP_DAYS),
            (local_dir(), "events_*.csv", EVENT_KEEP_DAYS)):
        try:
            for path in folder.glob(pattern):
                if path.stat().st_mtime < now - days * 86400:
                    path.unlink()
        except OSError:
            continue


# ------------------------------------------------------------------
def _name_of(tool: Any) -> str:
    if tool is None:
        return ""
    return str(getattr(tool, "display_name", "") or getattr(tool, "app_id", "")
               or "")


def _safe(text: str) -> str:
    """ファイル名に使える形へ。"""
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text or "").strip("_")


def _width(text: str) -> int:
    """表示幅 (全角は2)。見出しをそろえるため。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
