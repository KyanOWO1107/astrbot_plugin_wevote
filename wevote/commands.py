from __future__ import annotations

import re
from dataclasses import dataclass


class VoteError(ValueError):
    """可以直接向用户解释的投票输入或状态错误。"""


@dataclass(frozen=True)
class Draft:
    duration: int
    title: str
    options: tuple[str, ...]
    max_choices: int
    public: bool


def parse_create(text: str) -> Draft:
    parts = text.split(maxsplit=3)
    if len(parts) != 4:
        raise VoteError("格式：/投票 发起 10m 单选 公开 题目 | 选项1 | 选项2")
    duration_text, mode, visibility, content = parts
    match = re.fullmatch(r"([0-9]{1,7})([smh])", duration_text)
    if not match:
        raise VoteError("时长格式为 30s、10m 或 2h，范围为 1 秒至 7 天。")
    duration = int(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2]]
    if not 1 <= duration <= 7 * 86400:
        raise VoteError("投票时长须为 1 秒至 7 天。")
    title, *options = (part.strip() for part in content.split("|"))
    if not title or len(title) > 200:
        raise VoteError("题目须为 1 至 200 个字符。")
    if not 2 <= len(options) <= 20:
        raise VoteError("选项须为 2 至 20 个。")
    if any(not option or len(option) > 100 for option in options):
        raise VoteError("每个选项须为 1 至 100 个字符。")
    if len(set(options)) != len(options):
        raise VoteError("选项不能重复。")
    if mode == "单选":
        max_choices = 1
    else:
        match = re.fullmatch(r"多选([0-9]{1,2})", mode)
        if not match or not 2 <= int(match[1]) <= len(options):
            raise VoteError("模式为“单选”或“多选N”，N 须为 2 至选项数量。")
        max_choices = int(match[1])
    if visibility not in ("公开", "保密"):
        raise VoteError("公布模式须为“公开”或“保密”。")
    return Draft(duration, title, tuple(options), max_choices, visibility == "公开")


def parse_id(text: str) -> int:
    if not re.fullmatch(r"[0-9]{1,19}", text) or not 1 <= int(text) <= 2**63 - 1:
        raise VoteError("投票 ID 须为有效的正整数。")
    return int(text)


def parse_choices(tokens: list[str]) -> tuple[int, ...]:
    if not tokens or any(not re.fullmatch(r"[0-9]{1,2}", token) for token in tokens):
        raise VoteError("选项须为用空格分隔的编号，例如：/投票 选择 12 1 3。")
    return tuple(int(token) for token in tokens)
