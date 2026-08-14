import ast
from pathlib import Path
import textwrap
from typing import Dict, Optional
import unittest


ROOT = Path(__file__).resolve().parents[2]


def _method_source(path: Path, class_name: str, method_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name:
                    return ast.get_source_segment(source, child) or ""
    raise AssertionError(f"未找到 {class_name}.{method_name}")


def _build_order_detail_service() -> type:
    path = ROOT / "common/services/order_service.py"
    rate_limit_method = _method_source(path, "OrderDetailService", "_is_rate_limited_error")
    parser_method = _method_source(path, "OrderDetailService", "_parse_order_detail_response")

    class Logger:
        def info(self, *_args, **_kwargs):
            pass

        def error(self, *_args, **_kwargs):
            pass

    namespace = {"Dict": Dict, "Optional": Optional, "logger": Logger()}
    source = "class OrderDetailService:\n" + textwrap.indent(
        f"{rate_limit_method}\n\n{parser_method}", "    "
    )
    exec(source, namespace)
    return namespace["OrderDetailService"]


class OrderDetailRecoveryTest(unittest.TestCase):
    def test_rate_limited_response_is_retryable(self):
        service = _build_order_detail_service()
        self.assertTrue(
            service._is_rate_limited_error(
                ["FAIL_BIZ_COMMON_SYSTEM_ERROR2::闲鱼太累了,休息一会儿吧!"]
            )
        )
        self.assertFalse(service._is_rate_limited_error(["FAIL_SYS_TOKEN_EXPIRED::令牌过期"]))

    def test_order_detail_parser_extracts_specification(self):
        service = _build_order_detail_service()
        instance = object.__new__(service)
        instance.cookie_id = "seller"
        detail = instance._parse_order_detail_response(
            "order-1",
            {
                "data": {
                    "components": [
                        {
                            "render": "orderInfoVO",
                            "data": {
                                "buyerUserId": "buyer-1",
                                "itemInfo": {
                                    "itemId": "item-1",
                                    "buyAmount": "1",
                                    "price": "5.00",
                                    "skuInfo": "容量:2千张有效期1个月",
                                },
                            },
                        }
                    ]
                }
            },
        )

        self.assertEqual(detail["spec_name"], "容量")
        self.assertEqual(detail["spec_value"], "2千张有效期1个月")

    def test_redelivery_uses_order_detail_parser(self):
        source = _method_source(
            ROOT / "scheduler/app/services/scheduler/redelivery_task.py",
            "RedeliveryTask",
            "_process_order",
        )

        self.assertIn("OrderDetailService", source)
        self.assertIn("detail_service._parse_order_detail_response", source)
        self.assertNotIn("checker._parse_order_detail_response", source)

    def test_detail_fetch_retries_rate_limited_response_with_backoff(self):
        source = _method_source(
            ROOT / "common/services/order_service.py",
            "OrderDetailService",
            "_fetch_order_detail",
        )

        self.assertIn("rate_limited = self._is_rate_limited_error(ret_list)", source)
        self.assertIn("retry_delay = 0.5 if token_expired else 5 * (retry_count + 1)", source)
