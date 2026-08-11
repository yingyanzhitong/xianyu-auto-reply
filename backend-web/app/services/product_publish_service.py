"""
商品发布业务逻辑服务

功能：
1. 素材库 CRUD（创建/查询/更新/删除商品模板）
2. 提供素材字典转换工具，供发布执行链路复用
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.product_material import ProductMaterial


# ==================== 素材库服务 ====================

from common.utils.time_utils import safe_isoformat


SHOP_MATERIAL_FIELDS = (
    "shop_stock",
    "shop_shipping_mode",
    "shop_shipping_fee",
    "shop_support_pickup",
    "shop_fans_price_all",
    "shop_fans_price_old",
    "shop_fans_price_bought",
)
SHOP_SHIPPING_MODES = {"free", "distance", "fixed", "no_shipping"}
SHOP_FANS_PRICE_FIELDS = (
    "shop_fans_price_all",
    "shop_fans_price_old",
    "shop_fans_price_bought",
)


def validate_shop_material_config(data: dict) -> dict:
    """校验并规范化鱼小铺专属素材配置。"""
    config = {field: data.get(field) for field in SHOP_MATERIAL_FIELDS}
    mode = config["shop_shipping_mode"]
    if mode is not None and mode not in SHOP_SHIPPING_MODES:
        raise ValueError("鱼小铺发货方式不合法")

    stock = config["shop_stock"]
    if stock is not None:
        if isinstance(stock, bool):
            raise ValueError("鱼小铺库存必须是 1 到 9999 的整数")
        try:
            normalized_stock = int(stock)
        except (TypeError, ValueError):
            raise ValueError("鱼小铺库存必须是 1 到 9999 的整数") from None
        if normalized_stock != stock or not 1 <= normalized_stock <= 9999:
            raise ValueError("鱼小铺库存必须是 1 到 9999 的整数")
        config["shop_stock"] = normalized_stock

    def normalize_price(value: Any, field_label: str, maximum: Decimal | None = None) -> float | None:
        if value is None:
            return None
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError(f"{field_label}必须是有效金额") from None
        if not decimal_value.is_finite() or decimal_value <= 0:
            raise ValueError(f"{field_label}必须大于 0")
        if decimal_value.as_tuple().exponent < -2:
            raise ValueError(f"{field_label}最多保留两位小数")
        if maximum is not None and decimal_value > maximum:
            raise ValueError(f"{field_label}不能超过 {maximum}")
        return float(decimal_value)

    config["shop_shipping_fee"] = normalize_price(
        config["shop_shipping_fee"], "鱼小铺一口价邮费", Decimal("1000")
    )
    for field in SHOP_FANS_PRICE_FIELDS:
        config[field] = normalize_price(config[field], "鱼小铺粉丝价")

    if mode == "fixed" and config["shop_shipping_fee"] is None:
        raise ValueError("鱼小铺选择一口价时必须填写邮费")
    if mode != "fixed":
        config["shop_shipping_fee"] = None

    if config["shop_support_pickup"] is not None:
        config["shop_support_pickup"] = bool(config["shop_support_pickup"])
    return config


def _comparable_material_titles(title: str) -> List[str]:
    """返回用于同名判断的标题，兼容 Gamer520 自动添加的“秒发”前缀。"""
    normalized = re.sub(r"\s+", " ", str(title or "")).strip()
    without_prefix = re.sub(r"^【秒发】\s*", "", normalized).strip()
    return list(
        dict.fromkeys(
            value
            for value in (normalized, without_prefix)
            if value
        )
    )


class ProductMaterialService:
    """商品素材库 CRUD 服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, user_id: int, data: dict) -> ProductMaterial:
        """创建素材"""
        shop_config = validate_shop_material_config(data)
        material = ProductMaterial(
            user_id=user_id,
            title=data["title"],
            description=data["description"],
            price=float(data["price"]),
            original_price=float(data["original_price"]) if data.get("original_price") else None,
            category=data.get("category"),
            images=data.get("images", []),
            delivery_method=data.get("delivery_method", "express"),
            postage=float(data.get("postage", 0)),
            address=data.get("address"),
            brand=data.get("brand"),
            condition=data.get("condition", "全新"),
            remark=data.get("remark"),
            **shop_config,
        )
        self.session.add(material)
        await self.session.commit()
        await self.session.refresh(material)
        return material

    async def upsert_external(
        self,
        user_id: int,
        source_type: str,
        items: List[dict],
    ) -> List[dict]:
        """按外部来源商品ID批量幂等创建或更新素材。"""
        results: List[dict] = []
        for item in items:
            external_id = str(item["external_id"])
            stmt = select(ProductMaterial).where(
                ProductMaterial.user_id == user_id,
                ProductMaterial.source_type == source_type,
                ProductMaterial.source_item_id == external_id,
            )
            material = (await self.session.execute(stmt)).scalar_one_or_none()

            if material is None:
                comparable_titles = _comparable_material_titles(item["title"])
                duplicate_stmt = (
                    select(ProductMaterial)
                    .where(
                        ProductMaterial.user_id == user_id,
                        func.trim(ProductMaterial.title).in_(comparable_titles),
                    )
                    .order_by(ProductMaterial.id.asc())
                )
                duplicate = (
                    await self.session.execute(duplicate_stmt)
                ).scalars().first()
                if duplicate is not None:
                    results.append(
                        {
                            "external_id": external_id,
                            "material_id": duplicate.id,
                            "action": "skipped",
                            "content_hash": duplicate.source_content_hash,
                            "reason": "素材库已存在同名商品",
                        }
                    )
                    continue

                material = ProductMaterial(
                    user_id=user_id,
                    title=item["title"],
                    description=item["description"],
                    price=float(item["price"]),
                    original_price=None,
                    category=item.get("category"),
                    images=item.get("images", []),
                    delivery_method=item.get("delivery_method", "express"),
                    postage=float(item.get("postage", 0)),
                    address=item.get("address"),
                    brand=item.get("brand"),
                    condition=item.get("condition", "全新"),
                    remark=item.get("remark"),
                    source_type=source_type,
                    source_item_id=external_id,
                    source_content_hash=item["content_hash"],
                )
                self.session.add(material)
                await self.session.flush()
                action = "created"
            elif material.source_content_hash == item["content_hash"]:
                action = "unchanged"
            else:
                material.title = item["title"]
                material.description = item["description"]
                material.price = float(item["price"])
                material.category = item.get("category")
                material.images = item.get("images", [])
                material.delivery_method = item.get("delivery_method", "express")
                material.postage = float(item.get("postage", 0))
                material.address = item.get("address")
                material.brand = item.get("brand")
                material.condition = item.get("condition", "全新")
                material.remark = item.get("remark")
                material.source_content_hash = item["content_hash"]
                action = "updated"

            results.append(
                {
                    "external_id": external_id,
                    "material_id": material.id,
                    "action": action,
                    "content_hash": material.source_content_hash,
                }
            )

        await self.session.commit()
        return results

    async def list_materials(
        self, user_id: int = None, page: int = 1, page_size: int = 20,
        title: str = None, category: str = None, condition: str = None,
    ) -> Dict[str, Any]:
        """分页查询素材列表
        
        Args:
            user_id: 用户ID，为None时查询全部（管理员场景）
            title: 标题模糊搜索
            category: 分类筛选
            condition: 成色筛选
        """
        page = max(page, 1)
        page_size = page_size if page_size in (10, 20, 50, 100, 500, 1000) else 20

        base_cond = []
        if user_id is not None:
            base_cond.append(ProductMaterial.user_id == user_id)
        if title:
            base_cond.append(ProductMaterial.title.ilike(f"%{title}%"))
        if category:
            base_cond.append(ProductMaterial.category == category)
        if condition:
            base_cond.append(ProductMaterial.condition == condition)

        count_stmt = (
            select(func.count())
            .select_from(ProductMaterial)
            .where(*base_cond)
        )
        total = (await self.session.execute(count_stmt)).scalar() or 0

        stmt = (
            select(ProductMaterial)
            .where(*base_cond)
            .order_by(desc(ProductMaterial.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self.session.execute(stmt)).scalars().all()

        return {
            "list": [_material_to_dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if total else 0,
        }

    async def get(self, material_id: int, user_id: int = None) -> Optional[ProductMaterial]:
        """查询单条素材
        
        Args:
            material_id: 素材ID
            user_id: 用户ID，为None时不限用户（管理员场景）
        """
        conds = [ProductMaterial.id == material_id]
        if user_id is not None:
            conds.append(ProductMaterial.user_id == user_id)
        stmt = select(ProductMaterial).where(*conds)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_by_ids(self, material_ids: List[int], user_id: int) -> List[ProductMaterial]:
        if not material_ids:
            return []
        unique_ids = list(dict.fromkeys(material_ids))
        stmt = select(ProductMaterial).where(
            ProductMaterial.user_id == user_id,
            ProductMaterial.id.in_(unique_ids),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        material_map = {row.id: row for row in rows}
        return [material_map[mid] for mid in material_ids if mid in material_map]

    async def update(self, material_id: int, user_id: int = None, data: dict = None) -> Optional[ProductMaterial]:
        """更新素材（user_id=None时管理员可操作任意素材）"""
        data = data or {}
        material = await self.get(material_id, user_id)
        if not material:
            return None

        updatable = [
            "title", "description", "price", "original_price", "category",
            "images", "delivery_method", "postage", "address", "brand",
            "condition", "remark",
        ]
        nullable_fields = {"original_price", "category", "address", "brand", "remark"}
        for field in updatable:
            if field in data:
                value = data[field]
                if value is None and field not in nullable_fields:
                    continue
                if field in ("price", "original_price", "postage"):
                    value = float(value) if value is not None else None
                setattr(material, field, value)

        if any(field in data for field in SHOP_MATERIAL_FIELDS):
            shop_config = {
                field: getattr(material, field)
                for field in SHOP_MATERIAL_FIELDS
            }
            shop_config.update({field: data[field] for field in SHOP_MATERIAL_FIELDS if field in data})
            shop_config = validate_shop_material_config(shop_config)
            for field, value in shop_config.items():
                setattr(material, field, value)

        await self.session.commit()
        await self.session.refresh(material)
        return material

    async def delete(self, material_id: int, user_id: int = None) -> bool:
        """删除素材（user_id=None时管理员可操作任意素材）"""
        material = await self.get(material_id, user_id)
        if not material:
            return False
        await self.session.delete(material)
        await self.session.commit()
        return True

    async def batch_delete(self, material_ids: List[int], user_id: int = None) -> int:
        """批量删除素材，返回实际删除数量
        
        Args:
            material_ids: 素材ID列表
            user_id: 用户ID，为None时管理员可操作任意素材
        """
        if not material_ids:
            return 0
        from sqlalchemy import delete as sa_delete
        conds = [ProductMaterial.id.in_(material_ids)]
        if user_id is not None:
            conds.append(ProductMaterial.user_id == user_id)
        stmt = sa_delete(ProductMaterial).where(*conds)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount


# ==================== 工具函数 ====================

def _material_to_dict(m: ProductMaterial) -> dict:
    """将素材模型转为字典"""
    return {
        "id": m.id,
        "user_id": m.user_id,
        "title": m.title,
        "description": m.description,
        "price": float(m.price) if m.price is not None else 0,
        "original_price": float(m.original_price) if m.original_price is not None else None,
        "category": m.category,
        "images": m.images or [],
        "delivery_method": m.delivery_method,
        "postage": float(m.postage) if m.postage is not None else 0,
        "address": m.address,
        "brand": m.brand,
        "condition": m.condition,
        "remark": m.remark,
        "shop_stock": m.shop_stock,
        "shop_shipping_mode": m.shop_shipping_mode,
        "shop_shipping_fee": float(m.shop_shipping_fee) if m.shop_shipping_fee is not None else None,
        "shop_support_pickup": m.shop_support_pickup,
        "shop_fans_price_all": float(m.shop_fans_price_all) if m.shop_fans_price_all is not None else None,
        "shop_fans_price_old": float(m.shop_fans_price_old) if m.shop_fans_price_old is not None else None,
        "shop_fans_price_bought": float(m.shop_fans_price_bought) if m.shop_fans_price_bought is not None else None,
        "source_type": m.source_type,
        "source_item_id": m.source_item_id,
        "source_content_hash": m.source_content_hash,
        "created_at": safe_isoformat(m.created_at),
        "updated_at": safe_isoformat(m.updated_at),
    }
