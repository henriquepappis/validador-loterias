"""Lote de upload: um envio pode conter várias imagens."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # pending = aguardando revisão | confirmed = revisado e apurado
    status: Mapped[str] = mapped_column(String, default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    images: Mapped[list["Image"]] = relationship(  # noqa: F821
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="Image.id",
    )
