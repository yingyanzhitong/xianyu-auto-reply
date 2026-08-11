"""鱼小铺专用发布器工厂。"""
from __future__ import annotations

from pathlib import Path

from common.services.shop_xianyu_publisher import ShopXianyuPublisher


def create_shop_xianyu_publisher(static_root: str | Path | None = None) -> ShopXianyuPublisher:
    return ShopXianyuPublisher(static_root=static_root)
