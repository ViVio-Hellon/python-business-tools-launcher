#!/usr/bin/env python3
"""ランチャーの起動開始点 (要件定義書 §17 / §18 / 基盤仕様書 2.1)

`Start.vbs` (通常起動) と `start_debug.bat` (診断起動) の両方がここへ来る。
順に:

    1. 実行環境の確認   … Python の版・tkinter・書き込み権限・設定
    2. 多重起動の判定   … `launch_guard`
    3. 動いているツールの引き継ぎ
    4. ランチャーバーを出して常駐する

**画面を読み込む前に環境を確認する。** 先に読み込むと、tkinter が無い
端末での失敗が ImportError のトレースバックになり、利用者には何を
すればよいか分からない。

`Start.vbs` はコンソールを出さない (pythonw) ので、**標準出力は誰にも
見えない**。失敗はダイアログか、それも出せなければHTMLで伝える。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent
# 隣を import できるようにする。配布はフォルダごとコピーなので、
# インストール手順を増やさない
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# Pythonのバイトコードをアプリ本体の隣に書かせない (基盤仕様書 2.7)。
# 共有フォルダーに置いたランチャーを複数人が使うと、`__pycache__` の
# 書き込みが同期競合とアクセス権の問題を起こす。
#
# **環境変数 `PYTHONPYCACHEPREFIX` は使わない。** すでに動いている
# このプロセスには効かない(起動時にしか読まれない)うえ、設定すると
# `start.bat` で起こす業務ツールまで引き継いでしまい、**相手の
# バイトコードがランチャーのフォルダーに溜まる**。各ツールは自分の
# ローカル領域を持っているので、そこは荒らさない
if not sys.pycache_prefix:
    try:
        from launcher import app_config as _bootstrap_config

        _cache = _bootstrap_config.local_dir("pycache")
        _cache.mkdir(parents=True, exist_ok=True)
        sys.pycache_prefix = str(_cache)
    except Exception:                         # noqa: BLE001 - 失敗しても起動は続ける
        pass

from launcher import app_config  # noqa: E402
from launcher.logging_utils import configure_logging, get_logger  # noqa: E402

# 動かせる最低の Python。標準ライブラリだけで書いてあるので低くてよい
MIN_PYTHON = (3, 9)

_BOOT_AT = time.monotonic()


class StartupError(RuntimeError):
    """起動できない理由と、**次に何をすればよいか**。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# ------------------------------------------------------------------
# 1. 実行環境の確認 (基盤仕様書 ステップ5)
# ------------------------------------------------------------------
def check_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        raise StartupError(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上が必要です"
            f"(いまは {sys.version.split()[0]})",
            "https://www.python.org/downloads/ から新しいPythonを入れてください。")


def check_tkinter() -> None:
    """画面を出せるか。

    ランチャーは追加パッケージを使わないが、**tkinter だけは
    Python本体に含まれていないことがある**(Windowsの公式インストーラでは
    既定で入るが、外して入れることもできる)。ここで確かめておかないと、
    `pythonw` で起動したとき何も起きずに終わる。
    """
    import importlib.util

    if importlib.util.find_spec("tkinter") is None:
        raise StartupError(
            "tkinter が入っていないため、ランチャーの画面を出せません",
            "Pythonを入れ直し、インストーラの「tcl/tk and IDLE」に"
            "チェックを入れてください。\n"
            "画面なしで業務ツールを止めるだけなら stop.bat が使えます。")


def check_writable() -> Path:
    """ローカル領域を作れるか (基盤仕様書 2.7)。"""
    try:
        root = app_config.ensure_local_dirs()
    except OSError as exc:
        raise StartupError(
            f"作業用フォルダを作れません: {exc}",
            "書き込みの権限があるか、ディスクの空きがあるか確認してください。"
            ) from None

    probe = root / "runtime" / ".write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise StartupError(f"作業用フォルダに書き込めません: {root}",
                           str(exc)) from None
    return root


def check_config() -> None:
    """設定が読めているか。読めなくても既定値で動くが、記録は残す。"""
    error = app_config.load_error()
    if error:
        get_logger("launcher").warning("%s — 既定値で起動します", error)


def run_environment_checks() -> Path:
    """順に確認する。落ちたところで理由が分かるように分けてある。"""
    check_python_version()
    check_tkinter()
    root = check_writable()
    check_config()
    return root


# ------------------------------------------------------------------
# 2. 起動
# ------------------------------------------------------------------
def log_environment() -> None:
    """起動のたびに残す1枚 (要件定義書 §16 / 基盤仕様書 2.6)。"""
    log = get_logger("launcher")
    log.info("=" * 60)
    log.info("ランチャー起動: pid=%s", os.getpid())
    log.info("Python: %s (%s)", sys.version.split()[0], sys.executable)
    log.info("アプリ本体: %s", app_config.APP_ROOT)
    log.info("ローカル領域: %s", app_config.local_root())
    log.info("版: %s", app_config.version_label())
    try:
        from launcher import trace
        log.info("記録の置き場所: %s", trace.destination().describe())
    except Exception:                         # noqa: BLE001 - 記録で止めない
        log.warning("記録の置き場所を決められませんでした", exc_info=True)


def start() -> int:
    """ランチャーを出して常駐する。戻り値はプロセスの終了コード。

    **まず起動中の窓を出す。** 下ごしらえ (`boot.run`) には端末によって
    数秒かかり、そのあいだ何も出ないと、作業者には「押したのに何も
    起きない」としか見えない。
    """
    import boot
    import launch_guard

    log = get_logger("launcher")
    log_environment()
    try:
        from launcher import trace

        # Python ごと落ちたとき (窓が何も言わずに消える) の手がかりを残す
        trace.enable_crash_log()
    except Exception:                         # noqa: BLE001 - 記録で止めない
        log.warning("落ちたときの記録を用意できませんでした", exc_info=True)

    try:
        from launcher.ui.splash import run_with_splash

        result = run_with_splash(boot.STEPS, lambda report: boot.run(report))
    except ImportError:
        # 窓を出す部品が読めない。窓なしで進める
        result = boot.run()

    if not result.started:
        _show_message("すでに起動しています",
                      f"{result.reason}\n\n"
                      "すでに動いているランチャーバーを探してください"
                      "(画面の中央、またはツール使用中なら左下にあります)。")
        return 0

    # ここから先は**ロックを持っている**。失敗しても必ず外す
    try:
        # 下ごしらえで見つかった問題は、起動中の窓を閉じてから出す
        # (別スレッドからダイアログは出せない)
        for title, body in result.warnings:
            _show_message(title, body)

        from launcher import trace
        from launcher.ui import bar

        # 裏の処理で拾われなかった例外も、障害記録に残す
        trace.install_thread_hook()
        log.info("ランチャーバーを出します (%.2f秒)", time.monotonic() - _BOOT_AT)
        bar.run(result.manager)
        return 0
    except Exception as exc:
        # バーが落ちた。**黙って消えない** ── 利用者には「急に消えた」と
        # しか見えないので、障害記録を残す
        from launcher import trace
        log.exception("ランチャーが想定外の失敗で終了します")
        path = trace.incident(
            "ランチャーが想定外の失敗で終了した",
            whys=[f"ランチャーの中で想定外の例外が起きた "
                  f"({type(exc).__name__}: {exc})"],
            hints=["ランチャーの不具合の可能性があります。"
                   "この記録を開発担当へ渡してください。"],
            exc=exc)
        trace.event("ランチャー終了", trace.FAILED,
                    cause=f"{type(exc).__name__}: {exc}", incident=path)
        raise
    finally:
        launch_guard.remove_lock()
        log.info("ランチャーを終了しました")


# ------------------------------------------------------------------
# 3. 失敗の伝え方
# ------------------------------------------------------------------
def _show_message(title: str, body: str) -> bool:
    """ダイアログで伝える。出せなければ False。"""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(f"{app_config.display_name()} - {title}", body)
        root.destroy()
        # この Tk はメインスレッドで片付ける (`launcher/ui/splash.py` と同じ理由)
        del root
        import gc
        gc.collect()
        return True
    except Exception:                         # noqa: BLE001 - 伝え方で落ちない
        return False


def report_failure(error: StartupError) -> None:
    """コンソール・ダイアログ・ブラウザーの順に伝える。

    `Start.vbs` から起動された場合、標準出力は誰にも見えない。
    tkinter が無いことが理由のときはダイアログも出せないので、
    最後の手段としてHTMLを書いてブラウザーで開く。
    """
    print(f"\n[エラー] {error}", file=sys.stderr)
    if error.hint:
        print(error.hint, file=sys.stderr)

    try:
        from launcher import trace
        log_dir = str(trace.destination().path)
    except Exception:                         # noqa: BLE001
        try:
            log_dir = str(app_config.local_dir("logs"))
        except Exception:                     # noqa: BLE001
            log_dir = "(ローカル領域を特定できませんでした)"
    try:
        get_logger("launcher").error("起動に失敗: %s / %s", error, error.hint)
    except Exception:                         # noqa: BLE001
        pass
    incident = ""
    try:
        from launcher import trace
        incident = trace.incident(
            "ランチャーを起動できなかった",
            whys=[str(error)], hints=[error.hint] if error.hint else [])
        trace.event("ランチャー起動", trace.FAILED, cause=str(error),
                    detail=error.hint, incident=incident)
    except Exception:                         # noqa: BLE001 - 伝え方で落ちない
        pass

    body = f"{error}\n\n{error.hint}\n\nログ: {log_dir}"
    if incident:
        body += f"\n障害記録: {trace.real_path(incident)}"
    if _show_message("起動できませんでした", body):
        return
    try:
        import webbrowser

        page = _write_error_page(str(error), error.hint, log_dir)
        webbrowser.open(page.as_uri())
    except Exception as exc:                  # noqa: BLE001
        print(f"(エラー画面を出せませんでした: {exc})", file=sys.stderr)


def _write_error_page(message: str, hint: str, log_dir: str) -> Path:
    """起動に失敗したことを伝えるHTMLを書く。"""
    import html
    import tempfile

    try:
        target = app_config.local_dir("work") / "起動エラー.html"
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:                         # noqa: BLE001
        target = Path(tempfile.gettempdir()) / "launcher_起動エラー.html"

    target.write_text(f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>起動できませんでした</title>
<style>
 body{{margin:0;min-height:100vh;display:grid;place-items:center;
       background:#eef1f5;color:#101720;
       font-family:system-ui,"Yu Gothic UI","Meiryo UI",sans-serif;line-height:1.7}}
 .box{{width:min(560px,calc(100vw - 48px));background:#fff;border:1px solid #c9d2dc;
       border-radius:4px;padding:32px;box-shadow:0 6px 20px rgba(16,23,32,.08)}}
 h1{{margin:0 0 12px;font-size:19px;color:#b4232a}}
 .hint{{margin-top:16px;padding:14px;background:#fdeaea;border-left:4px solid #b4232a;
        border-radius:0 3px 3px 0;white-space:pre-wrap}}
 code{{font-family:ui-monospace,Consolas,monospace;font-size:13px;
       background:rgba(0,0,0,.06);padding:2px 5px;border-radius:2px;word-break:break-all}}
 dt{{color:#556171;font-size:13px;margin-top:12px}}
</style></head>
<body><main class="box">
<h1>ランチャーを起動できませんでした</h1>
<p>{html.escape(message)}</p>
{f'<div class="hint">{html.escape(hint)}</div>' if hint else ''}
<dt>ログの場所</dt>
<p><code>{html.escape(log_dir)}</code></p>
<dt>診断</dt>
<p>コンソールで詳しく見るには <code>start_debug.bat</code> を実行してください。</p>
</main></body></html>
""", encoding="utf-8")
    return target


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="業務ツール統合ランチャーを起動する")
    parser.add_argument("--check", action="store_true",
                        help="実行環境と設定の確認だけして終わる(診断用)")
    args = parser.parse_args(argv)

    configure_logging()

    if args.check:
        # 環境の問題は**全部見せる**。1つ直すたびに実行し直すのは手間
        from launcher import tool_registry

        problems: list[StartupError] = []
        for check in (check_python_version, check_tkinter, check_writable):
            try:
                check()
            except StartupError as exc:
                problems.append(exc)
        check_config()

        # どの版が入っていて、どの版が動いているか。**現場の調査は
        # ここから始まる**ので先頭に出す
        try:
            from launcher import version_info
            print(version_info.describe())
            print()
        except Exception as exc:              # noqa: BLE001 - 診断で落ちない
            print(f"バージョン情報を集められませんでした: {exc}")

        print(app_config.describe())
        print()
        # 画面の開き方は「切り替えのとき閉じられるか」を左右するので、
        # 診断で必ず見せる (要件定義書 §8.3)
        try:
            from launcher import browser
            print(browser.describe())
        except Exception as exc:              # noqa: BLE001 - 診断で落ちない
            print(f"ブラウザーの設定を読めませんでした: {exc}")

        # **端末によって使えないことがある。** 使えないと画面の照合が
        # できないので、配る前に分かるよう出しておく
        try:
            import process_manager
            ok, how = process_manager.inspection_status()
            print(f"プロセスの照合: {how}")
            if not ok:
                print("  ランチャーが今回開いた画面は閉じられますが、")
                print("  前回のランチャーが開いた画面は閉じられません。")
                print("  PIDでの停止もできません (stop.bat と停止要求は使えます)。")
        except Exception as exc:              # noqa: BLE001 - 診断で落ちない
            print(f"プロセスの照合を確かめられませんでした: {exc}")
        print()
        # 配布先フォルダ。**どの端末で作った設定が届いているか**を見せる
        try:
            from launcher import distribution
            print(distribution.describe())
            _, broken = distribution.load()
            if broken:
                problems.append(StartupError(
                    broken, "配布元の端末で［設定］→「配布先フォルダを作る」"
                            "から作り直してください"))
            _, broken = distribution.load_password()
            if broken:
                problems.append(StartupError(
                    broken, f"{distribution.PASSWORD_FILE} を消すと決め直せます"
                            " (ツールの設定は消えません)"))
        except Exception as exc:              # noqa: BLE001 - 診断で落ちない
            print(f"配布先フォルダを読めませんでした: {exc}")
        print()
        try:
            print(tool_registry.describe())
        except Exception as exc:              # noqa: BLE001 - 診断で落ちない
            print(f"設定を読めませんでした: {exc}")
        print()
        # 記録の置き場所と最近の障害。**調査はここから始まる**
        try:
            print(_describe_records())
        except Exception as exc:              # noqa: BLE001 - 診断で落ちない
            print(f"記録の置き場所を確かめられませんでした: {exc}")
        print()
        if problems:
            for problem in problems:
                print(f"[エラー] {problem}")
                if problem.hint:
                    print(f"         {problem.hint}")
            return 1
        print("実行環境の確認: 問題ありません")
        return 0

    try:
        run_environment_checks()
    except StartupError as exc:
        report_failure(exc)
        return 1

    try:
        return start()
    except StartupError as exc:
        report_failure(exc)
        return 1
    except KeyboardInterrupt:
        print("\n中断しました")
        return 0


def _describe_records() -> str:
    """診断に出す「記録」の段。置き場所と、最近の障害記録。"""
    from launcher import logging_utils, trace

    # **ほかのアプリから見た場所**で出す (Microsoft Store 版の Python は
    # AppData を振り替えるので、ランチャーに見える場所では探せない)
    real = trace.real_path
    dest = trace.destination()
    lines = ["[記録 (後追い・なぜなぜ用)]",
             f"  置き場所         : {dest.describe()}",
             f"  ランチャーのログ : {real(logging_utils.log_file_path())}",
             f"  出来事の一覧     : {real(trace.events_path())}",
             f"  障害記録         : {real(dest.path / 'incidents')}",
             f"  ツールの出力     : {real(trace.local_dir())}"
             " (tool_<アプリID>.out.log。いつも端末の中)"]
    if dest.problem:
        lines.append(f"  [注意] 指定された出力先に書けません: {dest.problem}")
    if trace.is_store_python() or trace.moved_by_python(trace.local_dir()):
        lines.append("  [注意] Microsoft Store 版の Python です。AppData に書いた"
                     "ファイルは上の場所へ振り替えられます"
                     " (ランチャーの［詳細］では、そのまま読めます)")
    recent = trace.recent_incidents(days=7)
    if recent:
        lines.append(f"  この7日の障害記録: {len(recent)}件")
        lines.extend(f"    {real(path)}" for path in recent[:5])
        if len(recent) > 5:
            lines.append(f"    ほか {len(recent) - 5}件")
    else:
        lines.append("  この7日の障害記録: ありません")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
