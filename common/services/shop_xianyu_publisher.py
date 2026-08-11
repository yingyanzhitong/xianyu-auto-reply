"""鱼小铺专用卖家页发布器。"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from loguru import logger

from common.services.promotion_xianyu_publisher import PromotionXianyuPublisher


SHOP_SHIPPING_LABELS = {
    "free": "包邮",
    "distance": "按距离计费",
    "fixed": "一口价",
    "no_shipping": "无需邮寄",
}


class ShopXianyuPublisher(PromotionXianyuPublisher):
    """在 seller.goofish.com 页面发布并填写鱼小铺专属字段。"""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._shop_item_data: dict[str, Any] = {}

    @staticmethod
    def parse_published_item_id(url: str | None) -> str | None:
        """解析鱼小铺成功页 hash/query 中的 itemId。"""
        matched = re.search(r"(?:[?&#]|^)(?:itemId|item_id|id)=([0-9]+)", str(url or ""), re.IGNORECASE)
        return matched.group(1) if matched else None

    async def publish_item(
        self,
        item_data: dict,
        cookie_data: dict,
        reuse_browser: bool = False,
        should_close: bool = True,
    ) -> dict:
        self._shop_item_data = dict(item_data)
        prepared_item_data = dict(item_data)
        prepared_item_data["stock"] = item_data.get("shop_stock") or 999
        return await super().publish_item(
            item_data=prepared_item_data,
            cookie_data=cookie_data,
            reuse_browser=reuse_browser,
            should_close=should_close,
        )

    async def _set_free_shipping(self) -> None:
        """替代父类固定包邮逻辑，按素材填写鱼小铺发货设置。"""
        if not self.page:
            raise Exception("浏览器页面未初始化")

        mode = self._shop_item_data.get("shop_shipping_mode") or "free"
        label = SHOP_SHIPPING_LABELS.get(mode)
        if not label:
            raise Exception("鱼小铺发货方式不合法")

        logger.info(f"\n[鱼小铺] 🚚 设置发货方式：{label}")
        target = await self._find_visible_text_target(label)
        if target is None:
            raise Exception(f"未找到鱼小铺发货方式：{label}")
        await target.click()
        await asyncio.sleep(0.5)

        if mode == "fixed":
            await self._fill_fixed_shipping_fee(self._shop_item_data.get("shop_shipping_fee"))
        if self._shop_item_data.get("shop_support_pickup") is True:
            await self._enable_pickup()

    async def _find_visible_text_target(self, text: str):
        if not self.page:
            return None
        selectors = [
            f'label:has-text("{text}")',
            f'[role="radio"]:has-text("{text}")',
            f'button:has-text("{text}")',
            f'span:has-text("{text}")',
            f'div:has-text("{text}")',
        ]
        for selector in selectors:
            try:
                candidates = await self.page.query_selector_all(selector)
            except Exception:
                continue
            for candidate in candidates:
                try:
                    if not await candidate.is_visible():
                        continue
                    candidate_text = re.sub(r"\s+", " ", await candidate.inner_text()).strip()
                    if candidate_text == text or (text in candidate_text and len(candidate_text) <= len(text) + 16):
                        return candidate
                except Exception:
                    continue
        return None

    async def _fill_fixed_shipping_fee(self, value: Any) -> None:
        if not self.page:
            raise Exception("浏览器页面未初始化")
        if value is None:
            raise Exception("鱼小铺选择一口价时必须填写邮费")

        selectors = [
            'input[placeholder*="运费"]',
            'input[aria-label*="运费"]',
            '[class*="freight"] input',
            '[class*="postage"] input',
            'xpath=//*[contains(normalize-space(.), "一口价")]/following::input[1]',
        ]
        fee_input = None
        for selector in selectors:
            try:
                candidate = await self.page.wait_for_selector(selector, timeout=3000)
                if candidate and await candidate.is_visible() and await candidate.is_enabled():
                    fee_input = candidate
                    break
            except Exception:
                continue
        if fee_input is None:
            raise Exception("未找到鱼小铺一口价邮费输入框")
        await fee_input.fill(str(value))
        logger.info(f"[鱼小铺] 一口价邮费已填写：{value}")

    async def _enable_pickup(self) -> None:
        target = await self._find_visible_text_target("支持自提")
        if target is None:
            raise Exception("未找到鱼小铺支持自提开关")
        await target.click()
        await asyncio.sleep(0.3)
        logger.info("[鱼小铺] 已开启支持自提")

    async def _click_publish_button(self, result: dict) -> None:
        """兼容 seller.goofish 成功页使用 itemId 而不是 id 参数。"""
        await super()._click_publish_button(result)
        if not self.page:
            return
        item_id = self.parse_published_item_id(self.page.url)
        if item_id:
            result["success"] = True
            result["message"] = "商品发布成功（鱼小铺成功页）"
            result["item_id"] = item_id
            result["item_url"] = f"https://www.goofish.com/item?id={item_id}"
            result["success_flag"] = "shop_success_page_item_id"
            logger.info(f"✅ 鱼小铺商品发布成功，itemId={item_id}")
