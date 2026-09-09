"""Configuração da sessão do PostgreSQL (SQLAlchemy)."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, future=True
)
Base = declarative_base()


def get_db() -> Iterator[Session]:
    """Dependency do FastAPI: fornece uma sessão e garante o fechamento."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Cria as tabelas declaradas nos models, se ainda não existirem."""
    import models  # noqa: F401  (registra os mappers no metadata)

    Base.metadata.create_all(bind=engine)
