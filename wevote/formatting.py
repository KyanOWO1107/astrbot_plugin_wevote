from datetime import datetime, timezone

from .store import Poll


HELP = """WeVote 群投票
/投票 发起 10m 单选 公开 题目 | 选项1 | 选项2
/投票 发起 2h 多选2 保密 题目 | 选项1 | 选项2 | 选项3
/投票 选择 ID 1 3
/投票 改票 ID 2 3
/投票 查看 ID
/投票 列表
/投票 结束 ID
/投票 取消 ID
时长：1 秒至 7 天，单位 s/m/h；选项：2 至 20 个。
发起、结束、取消：群主 / 群管理员 / AstrBot 管理员 / 白名单。
首次使用“选择”；已投票后使用“改票”。
保密统计仅向发起权限用户私聊发送。"""


def format_poll(poll: Poll, *, include_stats: bool = False) -> str:
    status = {"active": "进行中", "ended": "已结束", "cancelled": "已取消"}[poll.status]
    mode = "单选" if poll.max_choices == 1 else f"多选，最多 {poll.max_choices} 项"
    deadline = datetime.fromtimestamp(poll.deadline, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [f"投票 #{poll.id} · {status}", poll.title,
             f"{mode} · {'公开' if poll.public else '保密'}", f"截止：{deadline}",
             f"参与人数：{poll.participants}"]
    show_stats = include_stats and poll.status != "cancelled"
    for index, option in enumerate(poll.options, 1):
        line = f"{index}. {option}"
        if show_stats:
            count = poll.counts[index - 1]
            ratio = count / poll.participants * 100 if poll.participants else 0
            line += f"：{count} 票（{ratio:.1f}%）"
        lines.append(line)
    if show_stats:
        lines.append("比例按参与人数计算；多选比例合计可能超过 100%。")
        if not poll.participants:
            lines.append("暂无投票，无最高票选项。")
        else:
            highest = max(poll.counts)
            winners = [f"{index}. {option}" for index, (option, count)
                       in enumerate(zip(poll.options, poll.counts), 1) if count == highest]
            label = "并列最高票" if len(winners) > 1 else "最高票"
            lines.append(f"{label}：{' / '.join(winners)}（{highest} 票）")
    elif not poll.public and poll.status != "cancelled":
        lines.append("统计保密；有发起权限的用户可用“查看”请求私聊统计。")
    if poll.status == "active":
        lines.extend([f"投票：/投票 选择 {poll.id} 选项编号", f"改票：/投票 改票 {poll.id} 选项编号"])
    return "\n".join(lines)


def format_notification(poll: Poll) -> str:
    if poll.status == "cancelled":
        return f"投票 #{poll.id} 已取消：{poll.title}。不公布结果。"
    if not poll.public:
        return f"保密投票 #{poll.id} 已结束：{poll.title}。统计仅限发起权限用户查询。"
    return format_poll(poll, include_stats=True)


def format_list(polls: list[Poll]) -> str:
    if not polls:
        return "本群暂无进行中的投票。已结束的投票可通过 /投票 查看 ID 查询。"
    return "本群进行中的投票：\n" + "\n".join(
        f"#{poll.id} {poll.title}（{'公开' if poll.public else '保密'}，{poll.participants} 人参与）"
        for poll in polls
    )
