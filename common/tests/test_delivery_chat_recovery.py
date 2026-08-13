"""订单补发会话恢复与详情解析的回归测试。"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Optional
import unittest


ROOT = Path(__file__).resolve().parents[2]


def _function_source(path: Path, class_name: str, function_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == function_name:
                    return ast.get_source_segment(source, child) or ""
    raise AssertionError(f"未找到 {class_name}.{function_name}")


def _load_static_function(path: Path, class_name: str, function_name: str):
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    for node in module.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for child in node.body:
            if isinstance(child, ast.FunctionDef) and child.name == function_name:
                child.decorator_list = []
                namespace = {"Optional": Optional, "json": json}
                exec(compile(ast.fix_missing_locations(ast.Module(body=[child], type_ignores=[])), str(path), "exec"), namespace)
                return namespace[function_name]
    raise AssertionError(f"未找到 {class_name}.{function_name}")


class DeliveryChatRecoveryTest(unittest.TestCase):
    def test_create_chat_falls_back_to_recent_conversations(self):
        path = ROOT / "websocket/app/services/xianyu/xianyu_async.py"
        source = _function_source(path, "XianyuAsync", "create_chat_conversation")
        fallback = _function_source(path, "XianyuAsync", "_find_existing_chat_conversation")
        parser = _function_source(
            path, "XianyuAsync", "_extract_cid_from_conversation_list_response"
        )

        self.assertIn("await self._find_existing_chat_conversation", source)
        self.assertIn('"/r/Conversation/listNewestPagination"', fallback)
        self.assertIn("singleChatUserConversation", parser)
        self.assertIn("expected_users", parser)
        self.assertIn('extension.get("itemId")', parser)

    def test_conversation_parser_prefers_the_matching_item(self):
        parser = _load_static_function(
            ROOT / "websocket/app/services/xianyu/xianyu_async.py",
            "XianyuAsync",
            "_extract_cid_from_conversation_list_response",
        )
        response = {
            "body": {
                "userConvs": [
                    {
                        "singleChatUserConversation": {
                            "singleChatConversation": {
                                "cid": "older@goofish",
                                "pairFirst": "buyer@goofish",
                                "pairSecond": "seller@goofish",
                                "extension": {"itemId": "old-item"},
                            }
                        }
                    },
                    {
                        "singleChatUserConversation": {
                            "singleChatConversation": {
                                "cid": "target@goofish",
                                "pairFirst": "seller@goofish",
                                "pairSecond": "buyer@goofish",
                                "extension": '{"itemId":"new-item"}',
                            }
                        }
                    },
                ]
            }
        }

        self.assertEqual(
            parser(response, "buyer", "seller", "new-item"),
            "target",
        )

    def test_conversation_parser_rejects_another_buyer(self):
        parser = _load_static_function(
            ROOT / "websocket/app/services/xianyu/xianyu_async.py",
            "XianyuAsync",
            "_extract_cid_from_conversation_list_response",
        )
        response = {
            "body": {
                "userConvs": [
                    {
                        "singleChatConversation": {
                            "cid": "wrong@goofish",
                            "pairFirst": "other-buyer@goofish",
                            "pairSecond": "seller@goofish",
                        }
                    }
                ]
            }
        }

        self.assertIsNone(parser(response, "buyer", "seller", "new-item"))

    def test_redelivery_uses_the_detail_service_parser(self):
        source = _function_source(
            ROOT / "scheduler/app/services/scheduler/redelivery_task.py",
            "RedeliveryTask",
            "_process_order",
        )

        self.assertIn("OrderDetailService(", source)
        self.assertNotIn("checker._parse_order_detail_response", source)


if __name__ == "__main__":
    unittest.main()
