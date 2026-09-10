"""Imagem enviada (original) e seus recortes (um por bilhete)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from database import Base


class Image(Base):
    __tablename__ = "images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # "original" = foto enviada | "crop" = recorte de um bilhete
    kind: Mapped[str] = mapped_column(String, default="crop", nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("images.id", ondelete="CASCADE"), nullable=True
    )
    # Recorte confirmado pelo usuário para leitura.
    accepted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Caixa do recorte na foto original: [x, y, w, h] normalizado (0..1).
    box: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    batch: Mapped["Batch"] = relationship(back_populates="images")  # noqa: F821
    parent: Mapped["Image | None"] = relationship(
        "Image", remote_side="Image.id", back_populates="crops"
    )
    crops: Mapped[list["Image"]] = relationship(
        "Image",
        back_populates="parent",
        cascade="all, delete-orphan",
        order_by="Image.id",
    )
    tickets: Mapped[list["Ticket"]] = relationship(  # noqa: F821
        back_populates="image",
        cascade="all, delete-orphan",
        order_by="Ticket.id",
    )
