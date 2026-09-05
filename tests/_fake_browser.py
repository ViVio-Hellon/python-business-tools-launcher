#!/usr/bin/env python3
"""試験用の偽ブラウザー。

本物の Chrome / Edge の代わりに、`--app=URL --user-data-dir=…` を受け取って
**閉じられるまで居座る**だけのプロセス。ランチャーが見ているのは

    1. 起こしたプロセスのPID
    2. そのコマンドラインに専用プロファイルの道が入っていること

の2つだけなので、試験ではこれで足りる。開かれたURLを記録に残すので、
「いつ・どの画面を開いたか」を確かめられる。

**あえて子プロセスを作らない。** 中で別のプロセスを起こすと、
コマンドラインから専用プロファイルの印が消えて、ランチャーの照合が
通らなくなる ── 本物のブラウザーは自分のコマンドラインに
`--user-data-dir` を持ったまま動くので、そこを合わせている。
"""
from __future__ import annotations

import os
import sys
import time

RECORD_ENV = "FAKE_BROWSER_LOG"


def main() -> int:
    url = ""
    for arg in sys.argv[1:]:
        if arg.startswith("--app="):
            url = arg[len("--app="):]

    record = os.environ.get(RECORD_ENV)
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(f"{url}\n")

    # 閉じられるまで待つ。試験は必ず閉じるので、上限は保険
    time.sleep(600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
