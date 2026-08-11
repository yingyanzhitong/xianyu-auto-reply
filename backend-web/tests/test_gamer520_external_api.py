from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import HTTPException, Response
from jose import JWTError
from pydantic import ValidationError

from app.api import deps
from app.api.routes import admin as admin_routes
from app.api.routes.cards import BatchBindRequest
from app.api.routes.product_publish import (
    BatchPublishRequest,
    ExternalMaterialUpsertRequest,
    MaterialCreateRequest,
    ShopBatchPublishRequest,
)
from app.services.product_publish_service import (
    ProductMaterialService,
    _comparable_material_titles,
    validate_shop_material_config,
)
from app.services.account_service import AccountService
from common.models.user import UserStatus
from common.services.card_delivery_content import _build_api_params
from common.services.card_matcher import CardMatcher
from common.services.item_service import ItemService
from common.services.shop_fans_price_service import build_fans_price_payload
from common.services.shop_xianyu_publisher import ShopXianyuPublisher
from common.utils.security import (
    decrypt_api_key,
    encrypt_api_key,
    generate_api_key,
    hash_api_key,
    mask_api_key,
)
from common.utils.time_utils import get_beijing_now_naive


def external_item(index: int) -> dict:
    return {
        "external_id": str(index),
        "content_hash": f"{index:064x}",
        "title": f"【秒发】测试商品 {index}",
        "description": "测试简介",
        "price": 1,
        "images": [f"https://images.example/{index}.jpg"],
    }


class FakeResult:
    def __init__(self, user):
        self.user = user

    def scalar_one_or_none(self):
        return self.user

    def scalars(self):
        return self

    def first(self):
        return self.user


class FakeSession:
    def __init__(self, user):
        self.user = user
        self.commits = 0

    async def execute(self, _statement):
        return FakeResult(self.user)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _user):
        return None


class SequenceSession:
    def __init__(self, results):
        self.results = list(results)
        self.commits = 0

    async def execute(self, _statement):
        return FakeResult(self.results.pop(0))

    async def commit(self):
        self.commits += 1


class CapturingResult:
    rowcount = 1


class CapturingCardSession:
    def __init__(self):
        self.calls = []

    async def execute(self, statement, parameters):
        self.calls.append((str(statement), parameters))
        return CapturingResult()

    async def flush(self):
        return None


class ListResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class ListSession:
    def __init__(self, rows):
        self.rows = rows
        self.commits = 0
        self.added = []

    async def execute(self, _statement):
        return ListResult(self.rows)

    async def commit(self):
        self.commits += 1

    def add(self, value):
        self.added.append(value)


class EmptyQueryResult:
    def scalar(self):
        return 0

    def all(self):
        return []


class QueryCaptureSession:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(
            str(statement.compile(compile_kwargs={"literal_binds": True}))
        )
        return EmptyQueryResult()


class ApiKeySecurityTests(unittest.TestCase):
    def test_api_key_only_exposes_hash_and_mask(self):
        api_key = generate_api_key()
        digest = hash_api_key(api_key)

        self.assertTrue(api_key.startswith("xyk_"))
        self.assertEqual(len(digest), 64)
        self.assertNotIn(api_key, digest)
        self.assertTrue(mask_api_key(api_key).startswith(api_key[:8]))
        self.assertTrue(mask_api_key(api_key).endswith(api_key[-4:]))

    def test_api_key_ciphertext_can_be_reopened_by_admin(self):
        api_key = generate_api_key()
        ciphertext = encrypt_api_key(api_key, "stable-runtime-secret")

        self.assertNotIn(api_key, ciphertext)
        self.assertEqual(
            decrypt_api_key(ciphertext, "stable-runtime-secret"),
            api_key,
        )
        with self.assertRaisesRegex(ValueError, "无法解密"):
            decrypt_api_key(ciphertext, "different-secret")


class AdminApiKeyViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_can_view_full_api_key_without_response_caching(self):
        api_key = generate_api_key()
        user = SimpleNamespace(
            api_key_hash=hash_api_key(api_key),
            api_key_mask=mask_api_key(api_key),
            api_key_ciphertext=encrypt_api_key(
                api_key,
                admin_routes.settings.jwt_secret_key,
            ),
        )
        user_service = SimpleNamespace(get=AsyncMock(return_value=user))
        response = Response()

        result = await admin_routes.get_user_api_key(
            user_id=1,
            response=response,
            _=SimpleNamespace(),
            user_service=user_service,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.data["api_key"], api_key)
        self.assertFalse(result.data["requires_reset"])
        self.assertEqual(response.headers["cache-control"], "no-store")


class AccountIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_cookie_identity_is_rejected(self):
        existing = SimpleNamespace(
            id=1,
            owner_id=7,
            account_id="账号A",
            unb=None,
            cookie="unb=123456; tracknick=account-a",
        )
        service = AccountService(ListSession([existing]))

        with self.assertRaisesRegex(ValueError, "账号A"):
            await service.validate_cookie_identity(
                7,
                "unb=123456; tracknick=account-a",
                exclude_pk=2,
            )

    async def test_catalog_query_uses_exact_account_primary_key(self):
        session = QueryCaptureSession()
        service = ItemService(session)

        items, total = await service.list_items_paginated(
            owner_id=7,
            account_pk=42,
        )

        self.assertEqual(items, [])
        self.assertEqual(total, 0)
        self.assertTrue(
            all("xy_catalog_items.account_id = 42" in sql for sql in session.statements)
        )
        self.assertTrue(
            all("xy_catalog_items.owner_id = 7" in sql for sql in session.statements)
        )


class ExternalRequestValidationTests(unittest.TestCase):
    def test_external_material_limit_and_image_validation(self):
        request = ExternalMaterialUpsertRequest(
            source="gamer520",
            items=[external_item(index) for index in range(20)],
        )
        self.assertEqual(len(request.items), 20)

        with self.assertRaises(ValidationError):
            ExternalMaterialUpsertRequest(
                source="gamer520",
                items=[external_item(index) for index in range(21)],
            )

        invalid = external_item(1)
        invalid["images"] = ["file:///etc/passwd"]
        with self.assertRaises(ValidationError):
            ExternalMaterialUpsertRequest(
                source="gamer520",
                items=[invalid],
            )

    def test_batch_request_accepts_uuid_and_keeps_legacy_compatibility(self):
        request_id = "00000000-0000-4000-8000-000000000001"
        request = BatchPublishRequest(
            account_ids=["account-a"],
            material_ids=[1],
            request_id=request_id,
        )
        self.assertEqual(request.request_id, UUID(request_id))

        legacy = BatchPublishRequest(
            account_ids=["account-a"],
            material_ids=[1],
        )
        self.assertIsNone(legacy.request_id)

    def test_shop_batch_request_accepts_uuid(self):
        request_id = "00000000-0000-4000-8000-000000000002"
        request = ShopBatchPublishRequest(
            account_ids=["account-a"],
            material_ids=[1],
            request_id=request_id,
        )
        self.assertEqual(request.request_id, UUID(request_id))

    def test_fixed_shop_shipping_requires_fee(self):
        with self.assertRaises(ValidationError):
            MaterialCreateRequest(
                title="测试商品",
                description="测试描述",
                price=1,
                shop_shipping_mode="fixed",
            )

        material = MaterialCreateRequest(
            title="测试商品",
            description="测试描述",
            price=1,
            shop_stock=999,
            shop_shipping_mode="fixed",
            shop_shipping_fee=5.5,
            shop_fans_price_all=0.8,
        )
        self.assertEqual(material.shop_shipping_fee, 5.5)

    def test_shop_material_config_validates_precision_and_defaults(self):
        config = validate_shop_material_config({"shop_stock": 999})
        self.assertIsNone(config["shop_shipping_mode"])
        self.assertIsNone(config["shop_shipping_fee"])

        with self.assertRaisesRegex(ValueError, "最多保留两位小数"):
            validate_shop_material_config({"shop_fans_price_all": 1.234})

    def test_shop_fans_price_payload_and_success_url_parser(self):
        payload = build_fans_price_payload(
            "123456",
            {
                "shop_fans_price_all": 3.4,
                "shop_fans_price_old": 2.5,
                "shop_fans_price_bought": None,
            },
        )
        self.assertEqual(payload["itemId"], "123456")
        self.assertEqual(
            payload["fansPriceConfig"]["fansPriceList"],
            [
                {"fansGroup": "all", "priceString": "3.40"},
                {"fansGroup": "old", "priceString": "2.50"},
            ],
        )
        self.assertEqual(
            ShopXianyuPublisher.parse_published_item_id(
                "https://seller.goofish.com/?site=COMMONPRO#/seller-item/publish/success?itemId=123456"
            ),
            "123456",
        )

    def test_second_delivery_prefix_does_not_change_product_name(self):
        self.assertEqual(
            _comparable_material_titles("【秒发】  测试游戏 "),
            ["【秒发】 测试游戏", "测试游戏"],
        )

    def test_card_binding_accepts_item_title(self):
        request = BatchBindRequest(
            card_ids=[6],
            item_ids=["1067769058126"],
            item_title="【秒发】黄昏远征军",
        )
        self.assertEqual(request.item_title, "【秒发】黄昏远征军")

        with self.assertRaises(ValidationError):
            BatchBindRequest(
                card_ids=[6],
                item_ids=["1067769058126"],
                item_title="测" * 256,
            )


class CardBindingPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_binding_persists_item_title(self):
        session = CapturingCardSession()
        matcher = CardMatcher(session)

        result = await matcher.batch_bind_cards_to_items(
            user_id=1,
            card_ids=[6],
            item_ids=["1067769058126"],
            item_title="【秒发】黄昏远征军",
        )

        self.assertEqual(result, {"success_count": 1, "fail_count": 0})
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(
            session.calls[1][1]["item_title"],
            "【秒发】黄昏远征军",
        )


class CardApiDynamicParameterTests(unittest.TestCase):
    def test_item_title_is_replaced_in_nested_post_params(self):
        params = {
            "item_id": "{item_id}",
            "item_title": "{item_title}",
            "nested": [{"display_name": "商品：{item_title}"}],
        }

        result = _build_api_params(
            params,
            {
                "item_id": "1070619749960",
                "item_title": "【秒发】黄昏远征军",
            },
        )

        self.assertEqual(result["item_id"], "1070619749960")
        self.assertEqual(result["item_title"], "【秒发】黄昏远征军")
        self.assertEqual(
            result["nested"][0]["display_name"],
            "商品：【秒发】黄昏远征军",
        )


class CardRelationItemTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_relation_item_title_is_exposed_to_delivery_card(self):
        card = SimpleNamespace(
            id=6,
            user_id=1,
            item_id=None,
            name="下载源卡券",
            type="api",
            description=None,
            enabled=True,
            delay_seconds=0,
            delivery_count=0,
            is_multi_spec=False,
            spec_name=None,
            spec_value=None,
            api_config=None,
            text_content=None,
            data_content=None,
            image_url=None,
            image_urls=None,
            created_at=None,
            updated_at=None,
        )
        matcher = CardMatcher(SimpleNamespace())
        matcher._query_cards_with_source = AsyncMock(
            return_value=[
                (card, "own", 0, "【秒发】黄昏远征军"),
            ],
        )

        cards = await matcher.get_cards_by_item_id("1070619749960")

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["item_title"], "【秒发】黄昏远征军")


class ExternalMaterialDeduplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_same_name_material_is_skipped(self):
        duplicate = SimpleNamespace(
            id=88,
            source_content_hash="existing-hash",
        )
        session = SequenceSession([None, duplicate])
        service = ProductMaterialService(session)

        results = await service.upsert_external(
            user_id=1,
            source_type="gamer520",
            items=[external_item(1)],
        )

        self.assertEqual(results[0]["action"], "skipped")
        self.assertEqual(results[0]["material_id"], 88)
        self.assertIn("同名", results[0]["reason"])
        self.assertEqual(session.commits, 1)


class ShopMaterialUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_null_clears_shop_fields(self):
        material = SimpleNamespace(
            shop_stock=20,
            shop_shipping_mode="fixed",
            shop_shipping_fee=5.5,
            shop_support_pickup=True,
            shop_fans_price_all=3.0,
            shop_fans_price_old=2.0,
            shop_fans_price_bought=1.0,
        )
        session = FakeSession(None)
        service = ProductMaterialService(session)
        service.get = AsyncMock(return_value=material)

        updated = await service.update(
            material_id=1,
            user_id=1,
            data={
                "shop_stock": None,
                "shop_shipping_mode": None,
                "shop_support_pickup": None,
                "shop_fans_price_all": None,
                "shop_fans_price_old": None,
                "shop_fans_price_bought": None,
            },
        )

        self.assertIs(updated, material)
        self.assertIsNone(material.shop_stock)
        self.assertIsNone(material.shop_shipping_mode)
        self.assertIsNone(material.shop_shipping_fee)
        self.assertIsNone(material.shop_support_pickup)
        self.assertIsNone(material.shop_fans_price_all)


class RestAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_jwt_and_api_key_resolve_the_same_user(self):
        user = SimpleNamespace(
            id=1,
            status=UserStatus.ACTIVE,
            api_key_last_used_at=get_beijing_now_naive(),
        )
        session = FakeSession(user)

        with patch.object(deps, "decode_token", return_value={"sub": "1"}):
            jwt_user = await deps.get_current_user(
                token="jwt-token",
                api_key=None,
                session=session,
            )
        api_user = await deps.get_current_user(
            token=None,
            api_key="xyk_test",
            session=session,
        )

        self.assertIs(jwt_user, user)
        self.assertIs(api_user, user)

    async def test_invalid_jwt_does_not_fall_back_to_api_key(self):
        user = SimpleNamespace(
            id=1,
            status=UserStatus.ACTIVE,
            api_key_last_used_at=get_beijing_now_naive(),
        )
        session = FakeSession(user)

        with patch.object(
            deps,
            "decode_token",
            side_effect=JWTError("invalid"),
        ):
            with self.assertRaises(HTTPException) as raised:
                await deps.get_current_user(
                    token="invalid-jwt",
                    api_key="xyk_valid",
                    session=session,
                )
        self.assertEqual(raised.exception.status_code, 401)

    async def test_inactive_user_is_rejected_for_both_auth_methods(self):
        user = SimpleNamespace(status=UserStatus.INACTIVE)
        with self.assertRaises(HTTPException) as raised:
            await deps.get_current_active_user(user)
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
