"""鱼小铺专用卖家页发布器。"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from loguru import logger

from common.services.promotion_address_selector import (
    _get_shop_address_match_score,
    set_promotion_item_address,
)
from common.services.promotion_xianyu_publisher import PromotionXianyuPublisher


SHOP_SHIPPING_LABELS = {
    "free": "包邮",
    "distance": "按距离计费",
    "fixed": "一口价",
    "no_shipping": "无需邮寄",
}


class ShopXianyuPublisher(PromotionXianyuPublisher):
    """在 seller.goofish.com 页面发布并填写鱼小铺专属字段。"""

    PUBLISH_FORM_MARKERS = ("添加首图", "宝贝图片")

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._shop_item_data: dict[str, Any] = {}

    @staticmethod
    def parse_published_item_id(url: str | None) -> str | None:
        """解析鱼小铺成功页 hash/query 中的 itemId。"""
        matched = re.search(r"(?:[?&#]|^)(?:itemId|item_id|id)=([0-9]+)", str(url or ""), re.IGNORECASE)
        return matched.group(1) if matched else None

    @classmethod
    def is_quick_entry_page(cls, page_text: str | None) -> bool:
        """判断是否停留在鱼小铺首次进入页，而不是实际的发布表单。"""
        text = str(page_text or "")
        return "快速进入" in text and not any(marker in text for marker in cls.PUBLISH_FORM_MARKERS)

    async def _open_publish_page_with_cookie(self) -> None:
        """首次进入鱼小铺时自动通过“快速进入”页后再打开发布表单。"""
        await super()._open_publish_page_with_cookie()
        if not self.page:
            return

        try:
            page_text = await self.page.evaluate("() => document.body.innerText")
        except Exception:
            return
        if not self.is_quick_entry_page(page_text):
            return

        logger.info("[鱼小铺] 检测到首次进入页，点击“快速进入”")
        target = await self._find_visible_text_target("快速进入")
        if target is None:
            raise Exception("检测到鱼小铺快速进入页，但未找到“快速进入”按钮")
        await target.click()
        await asyncio.sleep(1.5)

        # 快速进入会写入卖家页初始化状态，随后重新打开目标路由，确保落到实际发布表单。
        await self.page.goto(
            self.PROMOTION_PUBLISH_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await asyncio.sleep(2)

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

    async def _set_item_address(self, item_data: dict):
        """沿用卖家页的元素定位，但采用标准发布一致的地址候选匹配语义。"""
        return await set_promotion_item_address(
            publisher=self,
            item_data=item_data,
            fallback_set_item_address=super(PromotionXianyuPublisher, self)._set_item_address,
            address_match_score=_get_shop_address_match_score,
            retry_detached_option_click=True,
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
