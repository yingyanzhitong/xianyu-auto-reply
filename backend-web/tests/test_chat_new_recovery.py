"""在线聊天实时订阅与 IM 自愈重连的源码契约回归测试。"""
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ChatNewRecoveryContractTests(unittest.TestCase):
    def test_browser_subscription_requires_a_healthy_im_connection(self):
        source = (ROOT / "backend-web/app/services/chat_new/im_session_manager.py").read_text()
        self.assertIn("async def ensure_connected", source)
        register_method = source.split("async def register_ws_client", 1)[1].split(
            "async def unregister_ws_client", 1
        )[0]
        self.assertIn("await self.ensure_connected(account_id)", register_method)

    def test_browser_ping_recovers_an_interrupted_im_connection(self):
        source = (ROOT / "backend-web/app/api/routes/chat_new_ws.py").read_text()
        ping_branch = source.split('if msg_type == "ping":', 1)[1].split("else:", 1)[0]
        self.assertIn("await manager.ensure_connected(account_id)", ping_branch)
        self.assertIn('"event": "pong"', ping_branch)

    def test_connected_accounts_restore_realtime_subscriptions_after_refresh(self):
        source = (ROOT / "frontend/src/pages/chat-new/ChatNew.tsx").read_text()
        self.assertIn("const connectedIds = accounts.filter", source)
        self.assertIn("setWsAccountIds((previous)", source)
        self.assertIn("accountIds: wsAccountIds", source)


if __name__ == "__main__":
    unittest.main()
