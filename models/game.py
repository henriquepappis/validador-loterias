"""Jogos individuais (A, B, C...) e dezenas."""
from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from database import Base


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    game_identifier: Mapped[str] = mapped_column(String, nullable=False)
    # Lista de inteiros jogados, ex: [4, 10, 24, 36, 40, 54]
    numbers: Mapped[list[int]] = mapped_column(JSON, nullable=False)

    ticket: Mapped["Ticket"] = relationship(back_populates="games")  # noqa: F821
