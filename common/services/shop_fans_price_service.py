"""鱼小铺商品发布后的粉丝价设置服务。"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from common.services.xianyu_mtop import mtop_call


FANS_PRICE_SET_API = "mtop.alibaba.idle.seller.platform.item.operate.fansprice.set"
FANS_PRICE_API_VERSION = "1.0"
FANS_PRICE_FIELDS = (
    ("shop_fans_price_all", "all"),
    ("shop_fans_price_old", "old"),
    ("shop_fans_price_bought", "buy"),
)


def build_fans_price_payload(item_id: str, item_data: dict) -> dict[str, Any] | None:
    """构造官方粉丝价接口载荷；未配置任何档位时返回 None。"""
    fans_price_list = []
    for field_name, fans_group in FANS_PRICE_FIELDS:
        value = item_data.get(field_name)
        if value is None:
            continue
        price = Decimal(str(value)).quantize(Decimal("0.01"))
        fans_price_list.append({"fansGroup": fans_group, "priceString": format(price, "f")})

    if not fans_price_list:
        return None
    return {
        "itemId": str(item_id),
        "fansPriceConfig": {
            "itemId": str(item_id),
            "fansPriceList": fans_price_list,
        },
    }


async def set_shop_fans_prices(
    *,
    account_id: str,
    cookies_str: str,
    owner_id: int,
    item_id: str,
    item_data: dict,
) -> dict[str, Any]:
    """设置已填写的三档粉丝价，不负责改变商品发布结果。"""
    payload = build_fans_price_payload(item_id=item_id, item_data=item_data)
    if payload is None:
        return {"success": True, "skipped": True, "message": "未设置粉丝价"}

    result = await mtop_call(
        account_id=account_id,
        cookies_str=cookies_str,
        api=FANS_PRICE_SET_API,
        version=FANS_PRICE_API_VERSION,
        data=payload,
        owner_id=owner_id,
    )
    if result.get("success"):
        return {"success": True, "skipped": False, "message": "粉丝价设置成功"}
    return {
        "success": False,
        "skipped": False,
        "message": result.get("error") or "粉丝价设置失败",
    }
