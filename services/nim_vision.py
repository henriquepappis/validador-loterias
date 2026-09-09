"""Integração com a API da NVIDIA NIM para OCR inteligente de bilhetes."""
from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Any

from openai import OpenAI

from config import settings

LOTTERY_TYPES = {"Mega-Sena", "Quina"}

_EXTRACTION_PROMPT = """
Você é um extrator de dados de bilhetes de loteria da Caixa Econômica Federal.
Analise a imagem do bilhete e devolva ESTRITAMENTE um objeto JSON válido,
sem texto antes ou depois, sem markdown, no formato:

{
  "lottery_type": "Mega-Sena" | "Quina",
  "draw_number": <inteiro do concurso>,
  "games": [
    { "identifier": "A", "numbers": [<inteiros das dezenas jogadas>] },
    { "identifier": "B", "numbers": [...] }
  ]
}

Regras:
- "lottery_type" deve ser exatamente "Mega-Sena" ou "Quina".
- "draw_number" é o número do concurso impresso no bilhete (apenas dígitos).
- Cada aposta do bilhete vira um item em "games", com o identificador impresso
  (A, B, C, ...) e a lista de dezenas como inteiros (ex: 4, não "04").
- Não invente dados. Se algo estiver ilegível, omita o item correspondente.
""".strip()


def _client() -> OpenAI:
    if not settings.nim_api_key:
        raise RuntimeError(
            "NIM_API_KEY não configurada. Defina no arquivo .env."
        )
    return OpenAI(base_url=settings.nim_base_url, api_key=settings.nim_api_key)


def _image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _strip_json(content: str) -> str:
    content = content.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", content, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    # fallback: recorta do primeiro { até o último }
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end != -1 and end > start:
        return content[start : end + 1]
    return content


def _coerce_ints(values: Any) -> list[int]:
    out: list[int] = []
    for value in values or []:
        try:
            out.append(int(str(value).strip()))
        except (TypeError, ValueError):
            continue
    return out


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    lottery_type = str(raw.get("lottery_type", "")).strip()
    if lottery_type not in LOTTERY_TYPES:
        lowered = lottery_type.lower()
        if "mega" in lowered:
            lottery_type = "Mega-Sena"
        elif "quina" in lowered:
            lottery_type = "Quina"
        else:
            raise ValueError(
                f"lottery_type inesperado retornado pela IA: {lottery_type!r}"
            )

    try:
        draw_number = int(str(raw.get("draw_number")).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("draw_number ausente ou inválido no retorno da IA") from exc

    games: list[dict[str, Any]] = []
    for index, game in enumerate(raw.get("games") or []):
        identifier = str(
            game.get("identifier") or chr(ord("A") + index)
        ).strip().upper()
        numbers = sorted(set(_coerce_ints(game.get("numbers"))))
        if numbers:
            games.append({"identifier": identifier, "numbers": numbers})

    if not games:
        raise ValueError("Nenhum jogo pôde ser extraído do bilhete")

    return {
        "lottery_type": lottery_type,
        "draw_number": draw_number,
        "games": games,
    }


def extract_ticket(image_path: str | Path) -> dict[str, Any]:
    """Lê um bilhete a partir da imagem salva e devolve os dados estruturados.

    Retorno::

        {
            "lottery_type": "Mega-Sena",
            "draw_number": 2750,
            "games": [{"identifier": "A", "numbers": [4, 10, 24, 36, 40, 54]}],
        }
    """
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(path)

    response = _client().chat.completions.create(
        model=settings.nim_model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _EXTRACTION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": _image_data_url(path)},
                    },
                ],
            }
        ],
        temperature=0.0,
        max_tokens=1024,
    )

    content = response.choices[0].message.content or ""
    try:
        raw = json.loads(_strip_json(content))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Resposta da IA não é JSON válido: {content[:500]!r}"
        ) from exc

    return _normalize(raw)
