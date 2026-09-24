"""［設定］を開く前のパスワード確認

**画面の部品には触らない。** 尋ね方 (`ask`) と知らせ方 (`tell`) を
受け取って、順番と判断だけをここで持つ。tkinter の無い環境でも
流れを確かめられるようにするため。

    ask(題名, 文言)        -> 入力された文字 / 取り消しなら None
    tell(種類, 題名, 文言)   種類は "info" か "error"

パスワードそのものは配布先フォルダ (`distribution/password.json`) に置く。
"""
from __future__ import annotations

from typing import Callable, Optional

from . import distribution
from .logging_utils import get_logger

log = get_logger("admin_lock")

Ask = Callable[[str, str], Optional[str]]
Tell = Callable[[str, str, str], None]

TITLE = "管理者パスワード"

# 打ち間違いを許す回数。超えたら画面を閉じる (開き直せば、また試せる)
MAX_ATTEMPTS = 3


def unlock(ask: Ask, tell: Tell) -> bool:
    """［設定］を開いてよいか。

    * パスワードがまだ無い → **その場で決めてもらう** (決めなければ開かない)
    * ある → 合っていれば開く
    * パスワードのファイルが壊れている → 開かない (確かめようがない)
    """
    _, problem = distribution.load_password()
    if problem:
        tell("error", TITLE,
             f"{problem}\n\n"
             "パスワードを確かめられないため、設定画面は開きません。\n"
             f"{distribution.PASSWORD_FILE} を消すと、次に開くときに"
             "決め直せます (ツールの設定は消えません)。")
        return False

    if not distribution.has_password():
        tell("info", TITLE,
             "設定を開くには管理者パスワードが必要です。\n"
             "はじめに決めてください。\n\n"
             "決めたパスワードは配布先フォルダに入り、フォルダーごと配ると\n"
             "配った先の端末でも同じパスワードになります。")
        return choose(ask, tell)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        text = ask(TITLE, "設定を開くには、管理者パスワードを入力してください")
        if text is None:
            return False
        if distribution.verify_password(text):
            log.info("管理者パスワードを確認しました")
            return True
        log.warning("管理者パスワードが違います (%d回目)", attempt)
        left = MAX_ATTEMPTS - attempt
        tell("error", TITLE,
             "パスワードが違います。"
             + (f"\n(あと{left}回)" if left else "\n設定画面は開きません。"))
    return False


def choose(ask: Ask, tell: Tell) -> bool:
    """新しいパスワードを決めてもらう。決まれば真。"""
    while True:
        first = ask(TITLE,
                    f"新しい管理者パスワード "
                    f"({distribution.MIN_PASSWORD_LENGTH}文字以上)")
        if first is None:
            return False
        problem = distribution.validate_new_password(first)
        if problem:
            tell("error", TITLE, problem)
            continue
        second = ask(TITLE, "確認のため、もう一度入力してください")
        if second is None:
            return False
        if first != second:
            tell("error", TITLE, "2回の入力が一致しません。もう一度決めてください。")
            continue
        break

    try:
        target = distribution.set_password(first)
    except (OSError, ValueError) as exc:
        # **保存できないのに「決めました」と言わない。** 次に開くとき
        # 「パスワードがありません」に戻ってしまう
        log.warning("管理者パスワードを保存できません: %s", exc)
        tell("error", TITLE,
             f"パスワードを保存できません。\n{exc}\n\n"
             f"保存先: {distribution.password_path()}\n"
             "ランチャーのフォルダーに書き込めるか確かめてください。")
        return False
    tell("info", TITLE, f"管理者パスワードを保存しました。\n{target}")
    return True
