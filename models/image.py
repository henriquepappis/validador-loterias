"""Imagem enviada; pode conter vários comprovantes (tickets)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Image(Base):
    __tablename__ = "images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    batch: Mapped["Batch"] = relationship(back_populates="images")  # noqa: F821
    tickets: Mapped[list["Ticket"]] = relationship(  # noqa: F821
        back_populates="image",
        cascade="all, delete-orphan",
        order_by="Ticket.id",
    )
