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
    nim_model: str = "meta/llama-3.2-90b-vision-instruct"

    # Armazenamento local de bilhetes
    storage_dir: str = "storage/tickets"

    # Segmentação: recorta cada bilhete da foto antes do OCR
    segmentation_enabled: bool = True
    # Área mínima de um bilhete, como fração da imagem (descarta ruído/bordas)
    segmentation_min_area_frac: float = 0.03
    # Lado mínimo (px) a que um recorte é ampliado para ajudar o OCR
    segmentation_upscale_to: int = 1700

    # Scraper da Caixa
    caixa_headless: bool = True
    caixa_timeout_ms: int = 30_000


settings = Settings()
