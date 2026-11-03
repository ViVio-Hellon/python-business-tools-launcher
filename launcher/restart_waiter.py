"""ランチャーの更新の「待ち番」(1.8.11)

管理者が［ランチャーの更新のため全 PC で閉じる］を押すと、各 PC のランチャーは
これを**その PC の中** (%LOCALAPPDATA%\\BusinessToolsLauncher\\runtime) にコピーして
動かしてから閉じる。待ち番は、ツール管理マスタの「ランチャー更新中」の印が外れたら
ランチャーをもう一度起動して、自分は終わる。

**標準ライブラリだけで動く。ランチャーの部品を読まない** ── 更新の途中の共有
フォルダーにある (古い版と新しい版の混ざった) ファイルを読み込まないため。

    python restart_waiter.py --master <マスタの道> --command <起動するコマンド (JSON)>
                             --pid-file <道> [--every 10] [--hours 24]

* 印が外れるまで `--every` 秒ごとにマスタを見る (読むだけ。毎回閉じる)
* マスタに届かない間も待ち続ける
* `--hours` を過ぎても印が外れなければ、起動せずに終わる (利用者が手で起動できる)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time

MARK = "__launcher__"


def marked(master: str) -> bool | None:
    """印があれば True、無ければ False、読めなければ None。"""
    if not os.path.isfile(master):
        return None
    try:
        conn = sqlite3.connect(f"file:{master}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT 1 FROM maintenance WHERE app_id = ?", (MARK,)).fetchone()
        return row is not None
    except sqlite3.Error as exc:
        if "no such table" in str(exc):
            return False
        return None
    finally:
        conn.close()


def launch(command: list[str]) -> None:
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags,
                     start_new_session=os.name != "nt")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--pid-file", default="")
    parser.add_argument("--every", type=float, default=10.0)
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args(argv)
    command = json.loads(args.command)
    if args.pid_file:
        try:
            with open(args.pid_file, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
        except OSError:
            pass
    deadline = time.monotonic() + args.hours * 3600
    try:
        while time.monotonic() < deadline:
            if marked(args.master) is False:
                launch(command)
                return 0
            time.sleep(args.every)
        return 1
    finally:
        if args.pid_file:
            try:
                with open(args.pid_file, encoding="utf-8") as handle:
                    mine = handle.read().strip() == str(os.getpid())
                if mine:
                    os.remove(args.pid_file)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
