"""Bilhetes cadastrados e imagens."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    lottery_type: Mapped[str] = mapped_column(String, nullable=False)
    draw_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    games: Mapped[list["Game"]] = relationship(  # noqa: F821
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="Game.game_identifier",
    )
