import asyncio
import importlib.util
import inspect
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class Plain:
    def __init__(self, text):
        self.text = text


class MessageChain:
    def __init__(self, chain):
        self.chain = chain


class GreedyStr(str):
    pass


def install_stubs():
    names = ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.message_components",
             "astrbot.api.star", "astrbot.core", "astrbot.core.star", "astrbot.core.star.filter",
             "astrbot.core.star.filter.command")
    modules = {name: types.ModuleType(name) for name in names}
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = types.SimpleNamespace(
        warning=lambda *args: None, exception=lambda *args: None)
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].MessageChain = MessageChain
    modules["astrbot.api.event"].filter = types.SimpleNamespace(command=lambda name: lambda f: f)
    modules["astrbot.api.message_components"].Plain = Plain

    class Star:
        def __init__(self, context):
            self.context = context

    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].StarTools = types.SimpleNamespace(get_data_dir=lambda name: None)
    modules["astrbot.core.star.filter.command"].GreedyStr = GreedyStr
    sys.modules.update(modules)


install_stubs()
import main as plugin_module


class FakeBot:
    def __init__(self):
        self.messages = []
        self.fail = False

    async def send_private_msg(self, **kwargs):
        if self.fail:
            raise RuntimeError("private send failed")
        self.messages.append(kwargs)
        return {"message_id": 123}


class Event:
    def __init__(self, user="100", role="member", admin=False, group="200", platform="bot1",
                 umo=None, raw=None):
        self.user, self.admin, self.group, self.platform = user, admin, group, platform
        self.unified_msg_origin = umo or f"{platform}:GroupMessage:{group}"
        self.message_obj = types.SimpleNamespace(raw_message=raw if raw is not None
                                                 else {"sender": {"role": role}})
        self.bot = FakeBot()
        self.platform_type = "aiocqhttp"
        self.stopped = False

    def get_sender_id(self): return self.user
    def get_group_id(self): return self.group
    def get_self_id(self): return "300"
    def get_platform_id(self): return self.platform
    def get_platform_name(self): return self.platform_type
    def is_admin(self): return self.admin
    def plain_result(self, text): return text
    def stop_event(self): self.stopped = True


class Context:
    def __init__(self):
        self.messages = []
        self.result = True
        self.fail_umos = set()

    async def send_message(self, umo, chain):
        if umo in self.fail_umos:
            raise RuntimeError("send failed")
        self.messages.append((umo, chain))
        return self.result


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        test_dir = Path(__file__).resolve().parents[1] / ".test-data"
        test_dir.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_dir)
        self.data_patch = patch.object(plugin_module.StarTools, "get_data_dir",
                                       return_value=Path(self.temp.name))
        self.data_patch.start()
        self.context = Context()
        self.plugin = plugin_module.WeVotePlugin(self.context, {"initiator_whitelist": ["400"]})
        self.clock = patch.object(plugin_module.time, "time", return_value=1000)
        self.clock.start()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.clock.stop()
        self.data_patch.stop()
        self.temp.cleanup()

    async def command(self, event, args):
        return "\n".join([text async for text in self.plugin.vote(event, args)])

    async def create(self, visibility="公开", event=None):
        return await self.command(event or Event(role="admin"),
                                  f"发起 30s 多选2 {visibility} 午饭 | 米饭 | 面条 | 饺子")

    async def test_all_authorized_roles_and_denial(self):
        for event in (Event(role="owner"), Event(role="admin"), Event(admin=True), Event(user="400")):
            text = await self.create(event=event)
            self.assertIn("进行中", text)
        for event in (Event(), Event(raw={"sender": {}}), Event(raw={"sender": "admin"})):
            self.assertIn("仅群主", await self.create(event=event))
        self.assertEqual(len(self.plugin.store.active(plugin_module.room_key(Event()), 1000)), 4)

    async def test_unauthorized_end_cancel_and_invalid_creation(self):
        await self.create()
        for command in ("结束 1", "取消 1"):
            self.assertIn("仅群主", await self.command(Event(), command))
        self.assertEqual(self.plugin.store.get(plugin_module.room_key(Event()), 1, 1000).status, "active")
        self.assertIn("时长", await self.command(Event(role="owner"), "发起 0s 单选 公开 题 | 一 | 二"))
        self.assertEqual(len(self.plugin.store.active(plugin_module.room_key(Event()), 1000)), 1)

    async def test_private_and_other_platform_rejected(self):
        private = Event(group="")
        other = Event(role="admin")
        other.platform_type = "discord"
        for event in (private, other):
            self.assertIn("仅支持", await self.command(event, "列表"))
            self.assertTrue(event.stopped)

    async def test_choice_change_workflow(self):
        await self.create()
        self.assertIn("还未投过", await self.command(Event(), "改票 1 1"))
        self.assertIn("投票成功", await self.command(Event(), "选择 1 1 3"))
        self.assertIn("请使用 /投票 改票", await self.command(Event(), "选择 1 2"))
        self.assertIn("改票成功", await self.command(Event(), "改票 1 2"))
        self.assertEqual(self.plugin.store.get(plugin_module.room_key(Event()), 1, 1000).counts, (0, 1, 0))

    async def test_same_group_user_session_umos_share_polls(self):
        creator = Event(role="admin", umo="bot1:GroupMessage:100_200")
        voter = Event(user="101", umo="bot1:GroupMessage:101_200")
        await self.create(event=creator)
        self.assertIn("投票成功", await self.command(voter, "选择 1 1"))
        await self.command(creator, "结束 1")
        await self.plugin.notify_pending()
        self.assertEqual(self.context.messages[0][0], creator.unified_msg_origin)

    async def test_cross_group_and_platform_isolation(self):
        await self.create()
        for event in (Event(group="201", role="admin"), Event(platform="bot2", role="admin")):
            for command in ("选择 1 1", "改票 1 1", "查看 1", "结束 1", "取消 1"):
                self.assertIn("本群不存在", await self.command(event, command))
            self.assertIn("暂无", await self.command(event, "列表"))

    async def test_confidential_stats_never_appear_in_group(self):
        await self.create("保密")
        await self.command(Event(user="500"), "选择 1 1")
        viewer = Event()
        basic = await self.command(viewer, "查看 1")
        self.assertNotIn("100.0%", basic)
        self.assertEqual(viewer.bot.messages, [])
        for viewer in (Event(role="admin"), Event(role="owner"), Event(admin=True), Event(user="400")):
            reply = await self.command(viewer, "查看 1")
            self.assertNotIn("100.0%", reply)
            self.assertIn("通过私聊发送", reply)
            sent = viewer.bot.messages[0]
            self.assertEqual(sent["user_id"], int(viewer.user))
            self.assertEqual(sent["self_id"], 300)
            self.assertIn("100.0%", sent["message"][0]["data"]["text"])

    async def test_private_failure_has_no_group_fallback(self):
        await self.create("保密")
        await self.command(Event(), "选择 1 1")
        viewer = Event(role="admin")
        viewer.bot.fail = True
        reply = await self.command(viewer, "查看 1")
        self.assertIn("私聊发送失败", reply)
        self.assertNotIn("100.0%", reply)
        self.assertEqual(self.context.messages, [])

    async def test_end_cancel_and_no_duplicate_delivery(self):
        await self.create()
        await self.create("保密")
        await self.command(Event(), "选择 1 1")
        self.assertIn("提前结束", await self.command(Event(user="400"), "结束 1"))
        self.assertIn("已取消", await self.command(Event(role="owner"), "取消 2"))
        await self.plugin.notify_pending()
        self.assertEqual(len(self.context.messages), 2)
        self.assertIsInstance(self.context.messages[0][1], MessageChain)
        self.assertIn("100.0%", self.context.messages[0][1].chain[0].text)
        self.assertIn("已取消", self.context.messages[1][1].chain[0].text)
        await self.plugin.notify_pending()
        self.assertEqual(len(self.context.messages), 2)

    async def test_failed_notification_does_not_block_other_groups_and_retries(self):
        await self.create()
        await self.create(event=Event(role="admin", group="201"))
        self.clock.stop()
        self.clock = patch.object(plugin_module.time, "time", return_value=1030)
        self.clock.start()
        self.context.fail_umos.add("bot1:GroupMessage:200")
        await self.plugin.notify_pending()
        self.assertEqual([p.id for p in self.plugin.store.pending(1030)], [1])
        self.assertEqual(len(self.context.messages), 1)
        self.context.fail_umos.clear()
        await self.plugin.notify_pending()
        self.assertEqual(self.plugin.store.pending(1030), [])
        self.assertEqual(len(self.context.messages), 2)

    async def test_false_send_result_keeps_pending(self):
        await self.create()
        await self.command(Event(role="admin"), "结束 1")
        self.context.result = False
        await self.plugin.notify_pending()
        self.assertEqual(len(self.plugin.store.pending(1000)), 1)
        self.context.result = True
        await self.plugin.notify_pending()
        self.assertEqual(self.plugin.store.pending(1000), [])

    async def test_notification_timeout_preserves_pending(self):
        await self.create()
        await self.command(Event(role="admin"), "结束 1")

        async def hangs(umo, chain):
            await asyncio.Event().wait()

        with patch.object(self.context, "send_message", hangs), patch.object(plugin_module, "SEND_TIMEOUT", 0.01):
            await self.plugin.notify_pending()
        self.assertEqual(len(self.plugin.store.pending(1000)), 1)

    async def test_initialize_idempotent_terminate_cancels_and_restart_settles(self):
        await self.create("保密")
        await self.plugin.initialize()
        task = self.plugin._task
        await self.plugin.initialize()
        self.assertIs(self.plugin._task, task)
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.plugin = plugin_module.WeVotePlugin(self.context, {"initiator_whitelist": []})
        self.clock.stop()
        self.clock = patch.object(plugin_module.time, "time", return_value=1031)
        self.clock.start()
        await self.plugin.initialize()
        await asyncio.sleep(0)
        await self.plugin.notify_pending()
        self.assertEqual(len(self.context.messages), 1)
        self.assertIn("保密投票 #1 已结束", self.context.messages[0][1].chain[0].text)

    async def test_concurrent_commands_keep_counts(self):
        await self.create()
        results = await asyncio.gather(*(self.command(Event(user=str(user)), "选择 1 1 2")
                                         for user in range(10)))
        self.assertTrue(all("投票成功" in result for result in results))
        self.assertEqual(self.plugin.store.get(plugin_module.room_key(Event()), 1, 1000).counts, (10, 10, 0))

    async def test_concurrent_duplicate_vote_is_rejected(self):
        await self.create()
        results = await asyncio.gather(self.command(Event(), "选择 1 1"),
                                       self.command(Event(), "选择 1 2"))
        self.assertEqual(sum("投票成功" in result for result in results), 1)
        self.assertEqual(sum("改票" in result for result in results), 1)
        self.assertEqual(self.plugin.store.get(plugin_module.room_key(Event()), 1, 1000).participants, 1)

    async def test_notification_calls_share_lock(self):
        await self.create()
        await self.command(Event(role="admin"), "结束 1")
        await asyncio.gather(self.plugin.notify_pending(), self.plugin.notify_pending())
        self.assertEqual(len(self.context.messages), 1)

    async def test_deadline_rejects_commands_even_before_scheduler_runs(self):
        await self.create()
        await self.command(Event(), "选择 1 1")
        with patch.object(plugin_module.time, "time", return_value=1030):
            for event, command in ((Event(user="101"), "选择 1 2"), (Event(), "改票 1 2"),
                                   (Event(role="admin"), "结束 1"), (Event(role="admin"), "取消 1")):
                self.assertIn("已结束", await self.command(event, command))
            self.assertIn("暂无", await self.command(Event(), "列表"))
            self.assertIn("已结束", await self.command(Event(), "查看 1"))

    async def test_greedy_annotation_and_package_import(self):
        signature = inspect.signature(plugin_module.WeVotePlugin.vote, eval_str=True)
        parameter = signature.parameters["args"]
        # AstrBot 对带默认值的参数只检查默认值，故 GreedyStr 不能用普通字符串默认值。
        self.assertIs(parameter.annotation, GreedyStr)
        self.assertIs(parameter.default, inspect.Parameter.empty)
        root = Path(plugin_module.__file__).parent
        package = types.ModuleType("wevote_plugin_test")
        package.__path__ = [str(root)]
        sys.modules[package.__name__] = package
        spec = importlib.util.spec_from_file_location("wevote_plugin_test.main", root / "main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.PLUGIN_NAME, "astrbot_plugin_wevote")
        self.assertIn("/投票 改票", await self.command(Event(), ""))
