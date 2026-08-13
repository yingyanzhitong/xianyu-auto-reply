"""鱼小铺专用卖家页发布器。"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from loguru import logger

from common.services.promotion_address_selector import (
    _get_shop_address_match_score,
    _is_detached_element_error,
    set_promotion_item_address,
)
from common.services.promotion_xianyu_publisher import PromotionXianyuPublisher
from common.utils.item_info_manager import ItemInfoManager
from common.utils.xianyu_utils import trans_cookies


SHOP_SHIPPING_LABELS = {
    "free": "包邮",
    "distance": "按距离计费",
    "fixed": "一口价",
    "no_shipping": "无需邮寄",
}


class ShopXianyuPublisher(PromotionXianyuPublisher):
    """在 seller.goofish.com 页面发布并填写鱼小铺专属字段。"""

    PUBLISH_FORM_MARKERS = ("添加首图", "宝贝图片", "宝贝描述", "商品规格", "发货设置")

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._shop_item_data: dict[str, Any] = {}
        self._listing_item_ids_before_publish: set[str] = set()
        self._verification_cookie = ""

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

    @classmethod
    def is_publish_form_ready(cls, page_text: str | None) -> bool:
        """识别鱼小铺当前版本的发布表单，不将有效 Cookie 误判为失效。"""
        text = str(page_text or "")
        return any(marker in text for marker in cls.PUBLISH_FORM_MARKERS)

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
        self._verification_cookie = str(cookie_data.get("cookie") or "")
        self._listing_item_ids_before_publish = await self._get_recent_listing_ids(
            self._verification_cookie
        )
        prepared_item_data = dict(item_data)
        prepared_item_data["stock"] = item_data.get("shop_stock") or 999
        result = await super().publish_item(
            item_data=prepared_item_data,
            cookie_data=cookie_data,
            reuse_browser=reuse_browser,
            should_close=should_close,
        )
        await self._confirm_unredirected_publish(result)
        return result

    async def _get_recent_listing_ids(self, cookie: str) -> set[str]:
        """读取在售首页商品 ID，用于确认本次是否新增了商品。"""
        if not cookie:
            return set()

        parsed_cookie = trans_cookies(cookie)
        user_id = parsed_cookie.get("unb") or parsed_cookie.get("cnaui")
        if not user_id:
            return set()

        manager = ItemInfoManager("shop_publish_verify", cookie)
        try:
            response = await manager.get_item_list_info(
                page_number=1,
                page_size=20,
                myid=user_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[鱼小铺] 发布前读取在售商品失败，跳过额外核验：{exc}")
            return set()
        finally:
            await manager.close()

        if not response.get("success"):
            logger.warning("[鱼小铺] 发布前未读取到在售商品，跳过额外核验")
            return set()
        return {
            str(item.get("id"))
            for item in response.get("items") or []
            if item.get("id")
        }

    @staticmethod
    def _find_new_matching_listing(
        items: list[dict[str, Any]],
        expected_title: str,
        existing_ids: set[str],
    ) -> dict[str, Any] | None:
        """从新出现的在售商品中匹配平台截断后的发布标题。"""
        normalized_expected = re.sub(r"\s+", "", str(expected_title or ""))
        if len(normalized_expected) < 8:
            return None

        for item in items:
            item_id = str(item.get("id") or "")
            normalized_title = re.sub(r"\s+", "", str(item.get("title") or ""))
            if not item_id or item_id in existing_ids or len(normalized_title) < 8:
                continue
            if normalized_expected.startswith(normalized_title) or normalized_title.startswith(normalized_expected):
                return item
        return None

    async def _confirm_unredirected_publish(self, result: dict[str, Any]) -> None:
        """卖家页未跳转时，以新增的在售商品为准确认发布结果。"""
        if result.get("success") or result.get("failure_reason") != "page_not_redirected":
            return

        # 闲鱼在发布后可能延迟写入列表，短暂等待后再查询一次。
        await asyncio.sleep(2)
        cookie = self._verification_cookie
        parsed_cookie = trans_cookies(cookie)
        user_id = parsed_cookie.get("unb") or parsed_cookie.get("cnaui")
        if not cookie or not user_id:
            return

        manager = ItemInfoManager("shop_publish_verify", cookie)
        try:
            response = await manager.get_item_list_info(
                page_number=1,
                page_size=20,
                myid=user_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[鱼小铺] 发布后读取在售商品失败，保留原结果：{exc}")
            return
        finally:
            await manager.close()

        if not response.get("success"):
            return
        item = self._find_new_matching_listing(
            response.get("items") or [],
            self._shop_item_data.get("title", ""),
            self._listing_item_ids_before_publish,
        )
        if item is None:
            return

        item_id = str(item["id"])
        result.update(
            success=True,
            message="商品发布成功（卖家页未跳转，已由在售列表核验）",
            item_id=item_id,
            item_url=f"https://www.goofish.com/item?id={item_id}",
            success_flag="shop_listing_verified",
        )
        result.pop("failure_reason", None)
        logger.info(f"✅ 鱼小铺商品已由在售列表核验成功，itemId={item_id}")

    async def _set_item_address(self, item_data: dict):
        """沿用卖家页的元素定位，但采用标准发布一致的地址候选匹配语义。"""
        return await set_promotion_item_address(
            publisher=self,
            item_data=item_data,
            fallback_set_item_address=super(PromotionXianyuPublisher, self)._set_item_address,
            address_match_score=_get_shop_address_match_score,
            retry_detached_option_click=True,
            allow_selected_address_alias=True,
            retry_unapplied_option_click=True,
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

    async def _click_publish_target(self, publish_btn, publish_btn_selector: str | None) -> None:
        """点击鱼小铺发布按钮，兼容按钮在表单校验后动态重绘。"""
        if publish_btn is None:
            raise Exception("未找到鱼小铺发布按钮")

        try:
            await publish_btn.click(timeout=2000)
            return
        except Exception as error:
            message = str(error).lower()
            is_dynamic_button_error = (
                _is_detached_element_error(error)
                or "element is not visible" in message
                or "element is not enabled" in message
            )
            if not is_dynamic_button_error or not self.page or not publish_btn_selector:
                raise

            logger.info("ℹ️ 鱼小铺发布按钮在点击前发生状态变化，重新查找可用按钮")
            refreshed_btn = await self._find_enabled_publish_button(publish_btn_selector)
            if refreshed_btn is None:
                raise Exception("鱼小铺发布按钮刷新后未恢复可用状态") from error
            await refreshed_btn.click(timeout=5000)

    async def _find_enabled_publish_button(self, selector: str):
        """从同一选择器的多个命中项中等待真实可用的发布按钮。"""
        if not self.page:
            return None

        for attempt in range(12):
            try:
                candidates = await self.page.query_selector_all(selector)
            except Exception:
                candidates = []

            for candidate in candidates:
                try:
                    if await candidate.is_visible() and await candidate.is_enabled():
                        return candidate
                except Exception:
                    continue

            if attempt < 11:
                await asyncio.sleep(0.5)
        return None

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
