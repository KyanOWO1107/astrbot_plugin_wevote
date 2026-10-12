import tempfile
import unittest
from pathlib import Path

from wevote.commands import VoteError, parse_choices, parse_create, parse_id
from wevote.formatting import format_notification, format_poll
from wevote.store import VoteStore


class ParsingTests(unittest.TestCase):
    def test_modes_and_units(self):
        for value, seconds in (("30s", 30), ("10m", 600), ("2h", 7200)):
            with self.subTest(value=value):
                draft = parse_create(f"{value} 多选2 保密 周末 活动 | 电影 | 爬山 | 聚餐")
                self.assertEqual(draft.duration, seconds)
                self.assertEqual(draft.max_choices, 2)
                self.assertFalse(draft.public)
                self.assertEqual(draft.title, "周末 活动")
        self.assertEqual(parse_create("1s 单选 公开 题目 | 一 | 二").max_choices, 1)

    def test_invalid_create(self):
        cases = ["", "10m 单选 公开", "0s 单选 公开 题 | 一 | 二",
                 "169h 单选 公开 题 | 一 | 二", "1d 单选 公开 题 | 一 | 二",
                 "10m 多选1 公开 题 | 一 | 二", "10m 多选3 公开 题 | 一 | 二",
                 "10m 单选 未知 题 | 一 | 二", "10m 单选 公开  | 一 | 二",
                 "10m 单选 公开 题 | 一", "10m 单选 公开 题 | 一 | ",
                 "10m 单选 公开 题 | 一 | 一",
                 f"10m 单选 公开 {'题'*201} | 一 | 二",
                 f"10m 单选 公开 题 | {'一'*101} | 二",
                 "10m 单选 公开 题 | " + " | ".join(map(str, range(21)))]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(VoteError):
                parse_create(value)

    def test_id_and_choice_parsing(self):
        self.assertEqual(parse_id("123"), 123)
        self.assertEqual(parse_choices(["1", "3"]), (1, 3))
        for value in ("0", "-1", "a", "1 2", str(2**63), "9" * 100):
            with self.subTest(value=value), self.assertRaises(VoteError):
                parse_id(value)
        for tokens in ([], ["-1"], ["1,3"], ["one"]):
            with self.subTest(tokens=tokens), self.assertRaises(VoteError):
                parse_choices(tokens)


class StoreTests(unittest.TestCase):
    def setUp(self):
        test_dir = Path(__file__).resolve().parents[1] / ".test-data"
        test_dir.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_dir)
        self.path = Path(self.temp.name) / "votes.sqlite3"
        self.store = VoteStore(self.path)
        self.room = "bot1/group1"
        self.now = 1000

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def create(self, text="30s 多选2 公开 题目 | 一 | 二 | 三", room=None):
        return self.store.create(room or self.room, "platform:GroupMessage:group1", "admin",
                                 parse_create(text), self.now)

    def test_single_choice_and_invalid_change_preserves_vote(self):
        poll = self.create("30s 单选 公开 题目 | 一 | 二")
        self.store.cast(self.room, poll.id, "user", (1,), self.now)
        for invalid in ((1, 2), (0,), (3,), (1, 1), ()):
            with self.subTest(choices=invalid), self.assertRaises(VoteError):
                self.store.cast(self.room, poll.id, "user", invalid, self.now, change=True)
            self.assertEqual(self.store.get(self.room, poll.id, self.now).counts, (1, 0))

    def test_explicit_change_replaces_and_never_adds_participant(self):
        poll = self.create()
        self.store.cast(self.room, poll.id, "a", (1, 2), self.now)
        changed = self.store.cast(self.room, poll.id, "a", (2, 3), self.now, change=True)
        self.assertEqual(changed.counts, (0, 1, 1))
        self.assertEqual(changed.participants, 1)

    def test_duplicate_cast_requires_change_command(self):
        poll = self.create()
        self.store.cast(self.room, poll.id, "a", (1,), self.now)
        with self.assertRaisesRegex(VoteError, "改票"):
            self.store.cast(self.room, poll.id, "a", (2,), self.now)
        self.assertEqual(self.store.get(self.room, poll.id, self.now).counts, (1, 0, 0))

    def test_change_requires_previous_vote(self):
        poll = self.create()
        with self.assertRaisesRegex(VoteError, "选择"):
            self.store.cast(self.room, poll.id, "a", (1,), self.now, change=True)
        self.assertEqual(self.store.get(self.room, poll.id, self.now).participants, 0)

    def test_multiselect_limits(self):
        poll = self.create()
        for choices in ((1, 2, 3), (1, 1), (4,), ()):
            with self.subTest(choices=choices), self.assertRaises(VoteError):
                self.store.cast(self.room, poll.id, "user", choices, self.now)
        result = self.store.cast(self.room, poll.id, "user", (3, 1), self.now)
        self.assertEqual(result.counts, (1, 0, 1))

    def test_deadline_boundary_rejects_votes_and_changes(self):
        poll = self.create()
        self.store.cast(self.room, poll.id, "a", (1,), 1029.999)
        for user, change in (("a", True), ("b", False)):
            with self.subTest(change=change), self.assertRaises(VoteError):
                self.store.cast(self.room, poll.id, user, (2,), 1030, change=change)
        result = self.store.get(self.room, poll.id, 1030)
        self.assertEqual(result.status, "ended")
        self.assertEqual(result.counts, (1, 0, 0))
        self.assertEqual([p.id for p in self.store.pending(1030)], [poll.id])

    def test_multiple_polls_and_room_isolation(self):
        first, second = self.create(), self.create()
        other = self.create(room="bot2/group1")
        self.assertEqual([p.id for p in self.store.active(self.room, 1001)], [first.id, second.id])
        for poll in (first, second):
            self.store.cast(self.room, poll.id, "a", (1,), 1001)
        for operation in (lambda: self.store.get(self.room, other.id, 1001),
                          lambda: self.store.cast(self.room, other.id, "a", (1,), 1001),
                          lambda: self.store.finish(self.room, other.id, 1001)):
            with self.assertRaisesRegex(VoteError, "本群不存在"):
                operation()

    def test_restart_restores_active_votes_and_non_reused_ids(self):
        poll = self.create()
        self.store.cast(self.room, poll.id, "a", (1, 3), 1001)
        self.store.close()
        self.store = VoteStore(self.path)
        self.assertEqual(self.store.get(self.room, poll.id, 1002).counts, (1, 0, 1))
        self.assertGreater(self.create().id, poll.id)

    def test_restart_settlement_and_notification_ack(self):
        poll = self.create()
        self.store.close()
        self.store = VoteStore(self.path)
        pending = self.store.pending(2000)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].status, "ended")
        self.assertEqual(pending[0].umo, poll.umo)
        self.store.mark_notified(poll.id)
        self.assertEqual(self.store.pending(2001), [])
        self.store.close()
        self.store = VoteStore(self.path)
        self.assertEqual(self.store.pending(2002), [])

    def test_early_end_and_cancel(self):
        for cancel, status in ((False, "ended"), (True, "cancelled")):
            poll = self.create()
            result = self.store.finish(self.room, poll.id, 1001, cancel=cancel)
            self.assertEqual(result.status, status)
            with self.assertRaises(VoteError):
                self.store.cast(self.room, poll.id, "a", (1,), 1002)
            with self.assertRaises(VoteError):
                self.store.finish(self.room, poll.id, 1002)

    def test_multiselect_percentages_and_ties(self):
        poll = self.create()
        result = self.store.cast(self.room, poll.id, "a", (1, 2), 1001)
        text = format_poll(result, include_stats=True)
        self.assertIn("1. 一：1 票（100.0%）", text)
        self.assertIn("2. 二：1 票（100.0%）", text)
        self.assertIn("3. 三：0 票（0.0%）", text)
        self.assertIn("并列最高票", text)
        self.assertIn("多选比例合计可能超过 100%", text)
        self.assertNotIn("admin", text)

    def test_empty_poll_and_single_winner(self):
        poll = self.create()
        self.assertIn("暂无投票，无最高票选项", format_poll(poll, include_stats=True))
        poll = self.store.cast(self.room, poll.id, "a", (2,), 1001)
        text = format_poll(poll, include_stats=True)
        self.assertIn("最高票：2. 二", text)
        self.assertNotIn("并列最高票", text)

    def test_confidential_and_cancelled_notifications_hide_counts(self):
        poll = self.create("30s 多选2 保密 题目 | 一 | 二 | 三")
        poll = self.store.cast(self.room, poll.id, "a", (1, 2), 1001)
        hidden = format_poll(poll)
        self.assertNotIn("100.0%", hidden)
        self.assertIn("参与人数：1", hidden)
        ended = self.store.finish(self.room, poll.id, 1002)
        self.assertNotIn("一", format_notification(ended))
        self.assertNotIn("100.0%", format_notification(ended))
        public = self.create()
        self.store.cast(self.room, public.id, "a", (1,), 1001)
        cancelled = self.store.finish(self.room, public.id, 1002, cancel=True)
        self.assertNotIn("100.0%", format_poll(cancelled, include_stats=True))
        self.assertEqual(format_notification(cancelled), f"投票 #{public.id} 已取消：题目。不公布结果。")
