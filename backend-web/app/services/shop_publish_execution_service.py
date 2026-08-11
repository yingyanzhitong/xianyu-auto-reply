"""鱼小铺批量发布独立执行服务。"""
from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.paths import STATIC_ROOT
from app.services.publish_address_service import PublishAddressService
from common.models.xy_account import XYAccount
from common.services.publish_log_service import PublishLogService
from common.services.shop_fans_price_service import set_shop_fans_prices
from common.services.shop_xianyu_publish_service import create_shop_xianyu_publisher


FANS_PRICE_FIELDS = (
    "shop_fans_price_all",
    "shop_fans_price_old",
    "shop_fans_price_bought",
)


class ShopPublishExecutorService:
    """鱼小铺多账号×多素材批量发布，不复用标准批量发布编排。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _get_account_map(self, account_ids: list[str], user_id: int) -> dict[str, XYAccount]:
        stmt = (
            select(XYAccount)
            .where(
                XYAccount.owner_id == user_id,
                XYAccount.account_id.in_(list(dict.fromkeys(account_ids))),
            )
            .order_by(desc(XYAccount.id))
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        account_map: dict[str, XYAccount] = {}
        for row in rows:
            account_map.setdefault(row.account_id, row)
        return account_map

    @staticmethod
    def _has_fans_price(material: dict) -> bool:
        return any(material.get(field) is not None for field in FANS_PRICE_FIELDS)

    async def batch_publish(
        self,
        *,
        user_id: int,
        account_ids: list[str],
        materials: list[dict],
        batch_id: str,
    ) -> dict[str, Any]:
        log_svc = PublishLogService(self.session)
        address_svc = PublishAddressService(self.session)
        account_map = await self._get_account_map(account_ids, user_id)
        success_count = 0
        failed_count = 0
        warning_count = 0
        log_ids: list[int] = []

        logger.info(
            f"鱼小铺批量发布开始: batch_id={batch_id}, 账号数={len(account_ids)}, 商品数={len(materials)}"
        )
        for account_id in account_ids:
            account = account_map.get(account_id)
            cookies_str = account.cookie if account and account.cookie else ""
            if not cookies_str:
                for material in materials:
                    log = await log_svc.create_log(
                        user_id=user_id,
                        account_id=account_id,
                        title=material.get("title", ""),
                        description=material.get("description", ""),
                        price=str(material.get("price", "")),
                        material_id=material.get("id"),
                        batch_id=batch_id,
                        status="failed",
                        error_message="账号不存在或无权使用",
                    )
                    log_ids.append(log.id)
                failed_count += len(materials)
                continue

            queue_state = await address_svc.build_queue_state(account_id)
            publisher = create_shop_xianyu_publisher(static_root=STATIC_ROOT)
            try:
                for index, material in enumerate(materials):
                    try:
                        resolved_address = await address_svc.resolve_publish_address(
                            account_id, material, queue_state
                        )
                    except ValueError as exc:
                        failed_count += 1
                        log = await log_svc.create_log(
                            user_id=user_id,
                            account_id=account_id,
                            title=material.get("title", ""),
                            description=material.get("description", ""),
                            price=str(material.get("price", "")),
                            material_id=material.get("id"),
                            batch_id=batch_id,
                            status="failed",
                            error_message=str(exc),
                        )
                        log_ids.append(log.id)
                        continue

                    publish_material = resolved_address.apply_to_item_data(material)
                    log = await log_svc.create_log(
                        user_id=user_id,
                        account_id=account_id,
                        title=material.get("title", ""),
                        description=material.get("description", ""),
                        price=str(material.get("price", "")),
                        material_id=material.get("id"),
                        batch_id=batch_id,
                        status="publishing",
                        **resolved_address.to_log_fields(),
                    )
                    log_ids.append(log.id)

                    try:
                        result = await publisher.publish_item(
                            item_data=publish_material,
                            cookie_data={"cookie": cookies_str},
                            reuse_browser=index > 0,
                            should_close=False,
                        )
                    except Exception as exc:  # noqa: BLE001
                        result = {"success": False, "message": str(exc)}
                        logger.exception(
                            f"鱼小铺发布单品异常: account={account_id}, title={material.get('title')}"
                        )

                    if not result.get("success"):
                        failed_count += 1
                        await log_svc.update_log(
                            log_id=log.id,
                            status="failed",
                            error_message=result.get("message") or "鱼小铺发布失败",
                        )
                    else:
                        success_count += 1
                        warning_message = None
                        if self._has_fans_price(material):
                            item_id = result.get("item_id")
                            if not item_id:
                                warning_message = "商品已发布成功，但未解析到商品ID，未设置粉丝价"
                            else:
                                try:
                                    fans_result = await set_shop_fans_prices(
                                        account_id=account_id,
                                        cookies_str=cookies_str,
                                        owner_id=user_id,
                                        item_id=str(item_id),
                                        item_data=material,
                                    )
                                    if not fans_result.get("success"):
                                        warning_message = (
                                            "商品已发布成功，但粉丝价设置失败："
                                            f"{fans_result.get('message') or '未知错误'}"
                                        )
                                except Exception as exc:  # noqa: BLE001
                                    logger.exception(f"鱼小铺粉丝价设置异常: item_id={item_id}")
                                    warning_message = f"商品已发布成功，但粉丝价设置异常：{exc}"

                        if warning_message:
                            warning_count += 1
                        await log_svc.update_log(
                            log_id=log.id,
                            status="success",
                            item_url=result.get("item_url"),
                            item_id=result.get("item_id"),
                            error_message=warning_message,
                        )

                    if index < len(materials) - 1:
                        await asyncio.sleep(3)
            finally:
                await publisher.close()

        logger.info(
            f"鱼小铺批量发布结束: batch_id={batch_id}, 成功={success_count}, "
            f"失败={failed_count}, 告警={warning_count}"
        )
        return {
            "success": True,
            "batch_id": batch_id,
            "total": len(account_ids) * len(materials),
            "success_count": success_count,
            "failed_count": failed_count,
            "warning_count": warning_count,
            "log_ids": log_ids,
        }
