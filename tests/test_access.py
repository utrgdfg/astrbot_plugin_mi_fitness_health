"""Regression tests for owner-only private-chat authorization."""

from __future__ import annotations

import unittest

from astrbot_plugin_mi_fitness_health.utils.access import (
    normalize_identifier,
    owner_access_denial_reason,
    owner_identifiers_match,
)


class OwnerAccessTest(unittest.TestCase):
    def test_sid_values_are_normalized(self) -> None:
        self.assertEqual(normalize_identifier(" UID: [1234567890] "), "1234567890")
        self.assertEqual(normalize_identifier("Bot ID：「示例机器人」"), "示例机器人")

    def test_platform_instance_is_required_for_private_health_data(self) -> None:
        self.assertFalse(
            owner_identifiers_match("1234567890", "", "1234567890", "示例机器人")
        )
        reason = owner_access_denial_reason(
            owner_platform_id="1234567890",
            owner_platform_instance_id="",
            sender_id="1234567890",
            platform_id="示例机器人",
            message_type="FriendMessage",
            is_private=True,
        )
        self.assertIn("owner_platform_instance_id", reason or "")

    def test_private_owner_with_matching_platform_instance_is_allowed(self) -> None:
        self.assertIsNone(
            owner_access_denial_reason(
                owner_platform_id="[1234567890]",
                owner_platform_instance_id="「示例机器人」",
                sender_id="1234567890",
                platform_id="示例机器人",
                message_type="FriendMessage",
                is_private=True,
            )
        )

    def test_uid_mismatch_is_not_reported_as_group_chat(self) -> None:
        reason = owner_access_denial_reason(
            owner_platform_id="1234567890",
            owner_platform_instance_id="示例机器人",
            sender_id="10000",
            platform_id="示例机器人",
            message_type="FriendMessage",
            is_private=True,
        )
        self.assertIn("UID", reason or "")
        self.assertIn("FriendMessage", reason or "")
        self.assertIn("不是群聊识别问题", reason or "")

    def test_platform_instance_mismatch_is_precise(self) -> None:
        reason = owner_access_denial_reason(
            owner_platform_id="1234567890",
            owner_platform_instance_id="另一个实例",
            sender_id="1234567890",
            platform_id="示例机器人",
            message_type="FriendMessage",
            is_private=True,
        )
        self.assertIn("Bot ID", reason or "")
        self.assertIn("不是群聊识别问题", reason or "")

    def test_runtime_identifiers_are_not_normalized(self) -> None:
        self.assertFalse(
            owner_identifiers_match(
                "1234567890", "示例机器人", "1234567890", "「示例机器人」"
            )
        )

    def test_group_message_is_rejected_as_group_message(self) -> None:
        reason = owner_access_denial_reason(
            owner_platform_id="1234567890",
            owner_platform_instance_id="示例机器人",
            sender_id="1234567890",
            platform_id="示例机器人",
            message_type="GroupMessage",
            is_private=False,
        )
        self.assertIn("当前消息类型为 GroupMessage", reason or "")
        self.assertIn("私聊", reason or "")


if __name__ == "__main__":
    unittest.main()
