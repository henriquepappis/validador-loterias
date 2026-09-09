"""Raspagem/Busca de resultados oficiais da Caixa, com cache em `draws`."""
from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from config import settings
from models.draw import Draw

logger = logging.getLogger(__name__)

# Slug usado tanto na API pública quanto nas páginas do portal.
_SLUGS = {"Mega-Sena": "megasena", "Quina": "quina"}
_API_URL = "https://servicebus2.caixa.gov.br/portaldeloterias/api/{slug}/{number}"
_RESULTS_PAGE = "https://loterias.caixa.gov.br/Paginas/{page}.aspx"
_PAGE_NAMES = {"Mega-Sena": "Mega-Sena", "Quina": "Quina"}

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


class DrawNotFoundError(RuntimeError):
    """Não foi possível obter o resultado do concurso em nenhuma fonte."""


def fetch_draw(lottery_type: str, draw_number: int, db: Session) -> Draw:
    """Devolve o `Draw` do concurso, usando cache do banco quando disponível."""
    cached = (
        db.query(Draw)
        .filter_by(lottery_type=lottery_type, draw_number=draw_number)
        .one_or_none()
    )
    if cached is not None:
        return cached

    numbers = _fetch_via_http(lottery_type, draw_number)
    if not numbers:
        numbers = _fetch_via_playwright(lottery_type, draw_number)
    if not numbers:
        raise DrawNotFoundError(
            f"Resultado do concurso {draw_number} ({lottery_type}) não encontrado."
        )

    draw = Draw(
        lottery_type=lottery_type,
        draw_number=draw_number,
        drawn_numbers=sorted(numbers),
    )
    db.add(draw)
    db.commit()
    db.refresh(draw)
    return draw


def _slug(lottery_type: str) -> str:
    try:
        return _SLUGS[lottery_type]
    except KeyError:
        raise ValueError(f"Modalidade não suportada: {lottery_type!r}") from None


def _parse_api_payload(payload: dict) -> list[int]:
    for key in ("listaDezenas", "dezenasSorteadasOrdemSorteio", "dezenas"):
        raw = payload.get(key)
        if raw:
            return [int(str(item).strip()) for item in raw]
    return []


def _fetch_via_http(lottery_type: str, draw_number: int) -> list[int]:
    """Fonte primária: API pública do Portal de Loterias da Caixa."""
    url = _API_URL.format(slug=_slug(lottery_type), number=draw_number)
    try:
        # A cadeia de certificados da Caixa costuma falhar em ambientes limpos.
        response = httpx.get(
            url,
            headers=_BROWSER_HEADERS,
            timeout=settings.caixa_timeout_ms / 1000,
            verify=False,
            follow_redirects=True,
        )
        response.raise_for_status()
        numbers = _parse_api_payload(response.json())
        if numbers:
            return numbers
        logger.warning("API da Caixa respondeu sem dezenas para %s", url)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Falha ao consultar API da Caixa (%s): %s", url, exc)
    return []


def _fetch_via_playwright(lottery_type: str, draw_number: int) -> list[int]:
    """Fallback: usa o Playwright para atingir a API por outra rota de rede.

    Import é preguiçoso para que a aplicação rode mesmo sem os browsers do
    Playwright instalados (`playwright install chromium`).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright não instalado; fallback indisponível.")
        return []

    api_url = _API_URL.format(slug=_slug(lottery_type), number=draw_number)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=settings.caixa_headless)
            try:
                page = browser.new_page(
                    user_agent=_BROWSER_HEADERS["User-Agent"]
                )
                # Estabelece origem/cookies visitando o portal antes da API.
                page.goto(
                    _RESULTS_PAGE.format(page=_PAGE_NAMES[lottery_type]),
                    timeout=settings.caixa_timeout_ms,
                    wait_until="domcontentloaded",
                )
                resp = page.request.get(
                    api_url, timeout=settings.caixa_timeout_ms
                )
                if resp.ok:
                    numbers = _parse_api_payload(resp.json())
                    if numbers:
                        return numbers
                logger.warning(
                    "Playwright: API respondeu status %s para %s",
                    resp.status,
                    api_url,
                )
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - fallback best-effort
        logger.warning("Falha no fallback Playwright: %s", exc)
    return []
