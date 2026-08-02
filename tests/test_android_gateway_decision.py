import unittest
from unittest import mock

from XianyuAutoAsync import XianyuLive, _android_gateway_owns_reply
from cookie_manager import android_gateway_owns_transport


def _live() -> XianyuLive:
    live = XianyuLive.__new__(XianyuLive)
    live.cookie_id = "account-001"
    live._check_buyer_blacklist_for_action = lambda **_kwargs: None
    return live


class AndroidGatewayDecisionTest(unittest.IsolatedAsyncioTestCase):
    def test_gateway_account_allowlist_is_explicit(self):
        with mock.patch.dict(
            "os.environ",
            {"ANDROID_GATEWAY_ACCOUNT_IDS": "account-001, account-002"},
            clear=False,
        ):
            self.assertTrue(_android_gateway_owns_reply("account-001"))
            self.assertFalse(_android_gateway_owns_reply("account-003"))
            self.assertTrue(android_gateway_owns_transport("account-002"))
            self.assertFalse(android_gateway_owns_transport("account-003"))

    async def test_keyword_decision_reuses_existing_priority_without_sending(self):
        live = _live()
        calls = []

        async def no_item(*_args):
            calls.append("item")
            return None

        async def keyword(*_args):
            calls.append("keyword")
            return "关键词命中回复"

        async def should_not_run(*_args, **_kwargs):
            raise AssertionError("lower-priority reply source should not run")

        async def filters(**_kwargs):
            return {"skip_auto_reply": False, "skip_ai_reply": False}

        live.get_item_specific_reply = no_item
        live.get_keyword_reply = keyword
        live.get_default_reply = should_not_run
        live.get_ai_reply = should_not_run
        live._apply_message_filters = filters
        live.send_msg = should_not_run

        with (
            mock.patch("XianyuAutoAsync.AUTO_REPLY", {"enabled": True}),
            mock.patch("XianyuAutoAsync.pause_manager.is_chat_paused", return_value=False),
        ):
            decision = await live.decide_chat_message_reply(
                send_user_name="买家甲",
                send_user_id="buyer-001",
                send_message="请问还在吗",
                item_id="item-001",
                chat_id="chat-001",
                msg_time="2026-07-28 16:00:00",
                reserve_default_reply=False,
            )

        self.assertEqual(
            decision,
            {
                "action": "reply",
                "text": "关键词命中回复",
                "source": "关键词",
                "reason": "matched",
            },
        )
        self.assertEqual(calls, ["item", "keyword"])

    async def test_gateway_decision_does_not_mark_default_reply_before_receipt(self):
        live = _live()
        record_reply_values = []

        async def no_reply(*_args):
            return None

        async def default_reply(*_args, record_reply=True):
            record_reply_values.append(record_reply)
            return "默认回复"

        async def filters(**_kwargs):
            return {"skip_auto_reply": False, "skip_ai_reply": False}

        live.get_item_specific_reply = no_reply
        live.get_keyword_reply = no_reply
        live.get_default_reply = default_reply
        live.get_ai_reply = no_reply
        live._apply_message_filters = filters

        with (
            mock.patch("XianyuAutoAsync.AUTO_REPLY", {"enabled": True}),
            mock.patch("XianyuAutoAsync.pause_manager.is_chat_paused", return_value=False),
        ):
            decision = await live.decide_chat_message_reply(
                send_user_name="买家甲",
                send_user_id="buyer-001",
                send_message="普通问题",
                item_id="item-001",
                chat_id="chat-001",
                msg_time="2026-07-28 16:00:00",
                reserve_default_reply=False,
            )

        self.assertEqual(decision["source"], "默认")
        self.assertEqual(record_reply_values, [False])


if __name__ == "__main__":
    unittest.main()
