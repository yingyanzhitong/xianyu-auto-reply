"""指定买家与商品的 AI 回复禁用记录。"""
from __future__ import annotations

from sqlalchemy import BigInteger, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from common.db.base_class import Base, TimestampMixin


class XYAIReplyBlock(TimestampMixin, Base):
    """按卖家账号、买家和商品精确禁用 AI 回复。"""

    __tablename__ = "xy_ai_reply_blocks"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "buyer_id",
            "item_id",
            name="uk_ai_reply_block_account_buyer_item",
        ),
        Index("idx_ai_reply_block_lookup", "account_id", "buyer_id", "item_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键ID")
    account_id: Mapped[str] = mapped_column(String(80), nullable=False, comment="闲鱼账号ID")
    buyer_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="买家ID")
    item_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="商品ID")
