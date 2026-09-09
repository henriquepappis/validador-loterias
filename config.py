"""Gerenciamento de variáveis de ambiente (.env)."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Banco de dados
    database_url: str = (
        "postgresql+psycopg2://loterias:loterias@localhost:5432/loterias"
    )

    # NVIDIA NIM Vision (SDK compatível com OpenAI)
    nim_api_key: str = ""
    nim_base_url: str = "https://integrate.api.nvidia.com/v1"
    nim_model: str = "meta/llama-3.2-11b-vision-instruct"

    # Armazenamento local de bilhetes
    storage_dir: str = "storage/tickets"

    # Scraper da Caixa
    caixa_headless: bool = True
    caixa_timeout_ms: int = 30_000


settings = Settings()
