"""Resultados oficiais dos concursos da Caixa."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from database import Base


class Draw(Base):
    __tablename__ = "draws"
    __table_args__ = (
        UniqueConstraint(
            "lottery_type", "draw_number", name="uq_draw_lottery_number"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lottery_type: Mapped[str] = mapped_column(String, nullable=False)
    draw_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Lista de inteiros sorteados pela Caixa
    drawn_numbers: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
