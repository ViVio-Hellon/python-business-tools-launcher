"""どの PC で、どの版のランチャーが動いているか (1.8.14 / 1.8.18 で作り直し)

ランチャーは起動しっぱなしなので、共有フォルダーの版を新しくしても、各 PC では
**起動したときの版**が動き続ける。管理者の「ランチャーの更新」の窓で「返事なし」の
PC が、古い版で動いているのか、起動していないのかを見分けるために書く。

1.8.14 ではツール管理マスタ (SQLite) の表に 1 分ごとに書いていたが、

* マスタはツールの一覧そのもの。何十台もの PC が共有フォルダーの同じファイルへ
  1 分ごとに書くと、待ちが増え、壊れるおそれも増える
* 「最近動いていたか」を、**書いた PC の時計**の時刻で判断していた (時計のずれた
  PC を「動いていない」と取り違える)

ので、マスタの隣のフォルダーに **PC ごとの小さなファイル** を書く形にした。
「いつ書いたか」は**ファイルの更新時刻** (共有サーバーが付ける) で比べ、
「いま」は**いちばん新しいファイルの更新時刻**とする (どの PC の時計も使わない)。

    <マスタのフォルダー>/launcher_動作中/<PC名>.json
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

FOLDER_NAME = "launcher_動作中"
# 書く間 (秒)。管理者の窓は、いちばん新しいものよりこれだけ古ければ「動いていない」
BEAT_SEC = 60.0
FRESH_SEC = 5 * 60.0

_UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')


@dataclass(frozen=True)
class Seen:
    login_id: str
    pc_name: str
    version: str
    written_at: float          # ファイルの更新時刻 (共有サーバーの時計)

    def behind(self, now: float) -> float:
        """いちばん新しいものより何秒古いか。"""
        return max(0.0, now - self.written_at)


def folder_for(master: Path) -> Path:
    return Path(master).parent / FOLDER_NAME


def _file_name(pc_name: str) -> str:
    return (_UNSAFE.sub("_", pc_name).strip("._") or "PC") + ".json"


def write(master: Path, login_id: str, pc_name: str, version: str) -> Path:
    """この PC の「動いている」を書く (置き換えで書くので、読む側が半端を読まない)。"""
    folder = folder_for(master)
    folder.mkdir(exist_ok=True)
    target = folder / _file_name(pc_name)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps({"login_id": login_id, "pc_name": pc_name,
                                "version": version}, ensure_ascii=False),
                    encoding="utf-8")
    os.replace(temp, target)
    return target


def read(master: Path) -> tuple[list[Seen], Optional[float]]:
    """(各 PC の「動いている」, いま)。「いま」はいちばん新しい更新時刻。無ければ None。"""
    folder = folder_for(master)
    seen: list[Seen] = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return [], None
    for entry in entries:
        if not entry.name.endswith(".json") or entry.name.startswith("."):
            continue
        try:
            written = entry.stat().st_mtime
            data = json.loads(Path(entry.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        seen.append(Seen(str(data.get("login_id") or ""), str(data.get("pc_name") or ""),
                         str(data.get("version") or ""), written))
    now = max((s.written_at for s in seen), default=None)
    return seen, now
