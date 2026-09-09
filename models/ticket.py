"""Comprovante individual lido de uma imagem."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image_id: Mapped[int] = mapped_column(
        ForeignKey("images.id", ondelete="CASCADE"), nullable=False
    )
    # Nulos enquanto o comprovante está pendente de revisão.
    lottery_type: Mapped[str | None] = mapped_column(String, nullable=True)
    draw_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # pending = precisa de revisão | confirmed = revisado pelo usuário
    status: Mapped[str] = mapped_column(String, default="pending", nullable=False)
    # Avisos da leitura/validação exibidos na tela de revisão.
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    image: Mapped["Image"] = relationship(back_populates="tickets")  # noqa: F821
    games: Mapped[list["Game"]] = relationship(  # noqa: F821
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="Game.game_identifier",
    )
