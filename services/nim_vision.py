"""Integração com a API da NVIDIA NIM para OCR inteligente de comprovantes.

A imagem enviada pode conter VÁRIOS comprovantes; `extract_tickets` devolve
uma lista, um item por comprovante, já com avisos de validação.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Any

from openai import OpenAI

from config import settings
from services.lottery import clean_numbers, normalize_lottery_type, validate_numbers

_EXTRACTION_PROMPT = """
Você é um extrator de dados de comprovantes de loteria da Caixa Econômica
Federal (apenas Mega-Sena e Quina). A imagem normalmente mostra UM comprovante
(pode, excepcionalmente, mostrar mais de um). Cada comprovante tem uma ou mais
apostas — sequências de dezenas marcadas, geralmente rotuladas A, B, C...

Devolva ESTRITAMENTE um JSON válido, sem markdown e sem qualquer texto fora do
JSON, no formato:

{
  "tickets": [
    {
      "lottery_type": "Mega-Sena" | "Quina",
      "draw_number": <inteiro do concurso>,
      "games": [
        { "numbers": [<inteiros das dezenas de uma aposta>] }
      ]
    }
  ]
}

Regras:
- Um item em "tickets" para CADA comprovante visível (quase sempre apenas um).
- "draw_number" é o número que aparece depois de "CONC" ou "Concurso", perto do
  valor "TOTAL R$". NÃO use a data (ex.: 09SET2026) nem o ano como concurso.
- Cada aposta é uma linha que começa com uma letra (A, B, C, ...) seguida das
  dezenas, cada uma com dois dígitos. Transcreva TODAS as dezenas da linha,
  inclusive a primeira. Mega-Sena tem 6+ dezenas por linha; Quina tem 5+.
- Em "games", liste TODAS as apostas do comprovante, na ordem impressa, sem
  repetir nenhuma.
- "numbers" são inteiros, sem zero à esquerda (4, não "04") e sem sufixos.
- Mega-Sena: dezenas de 1 a 60. Quina: dezenas de 1 a 80.
- Não invente dados. Se uma dezena estiver ilegível, omita-a.
- Responda apenas com o JSON.
""".strip()


def _client() -> OpenAI:
    if not settings.nim_api_key:
        raise RuntimeError("NIM_API_KEY não configurada. Defina no arquivo .env.")
    return OpenAI(base_url=settings.nim_base_url, api_key=settings.nim_api_key)


def _image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _strip_json(content: str) -> str:
    content = content.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", content, re.DOTALL)
    if fence:
        content = fence.group(1).strip()
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end != -1 and end > start:
        return content[start : end + 1]
    return content


def _repair_json(s: str) -> str:
    """Conserta os erros mais comuns do modelo antes do parse."""
    # zero à esquerda em contexto numérico: [01, 04] -> [1, 4]
    s = re.sub(r"(?<=[\[,\s])0+(\d)", r"\1", s)
    # token com sufixo alfabético: 5a, 12o -> 5, 12
    s = re.sub(r"(?<=[\[,\s])(\d+)[A-Za-zº°ª]+(?=[,\]\s])", r"\1", s)
    # vírgula sobrando antes de fechar
    s = re.sub(r",(\s*[\]}])", r"\1", s)
    return s


def _loads(content: str) -> dict[str, Any]:
    stripped = _strip_json(content)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return json.loads(_repair_json(stripped))


def _to_int(value: Any) -> int | None:
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


def _game_numbers(raw_game: Any) -> list[int]:
    """Aceita {'numbers': [...]}, {'dezenas': [...]}, [...] ou '01 02 03'."""
    if isinstance(raw_game, dict):
        for key in ("numbers", "dezenas", "numeros", "values"):
            if key in raw_game:
                return clean_numbers(raw_game[key])
        return []
    return clean_numbers(raw_game)


def _normalize_ticket(raw: Any, index: int) -> dict[str, Any]:
    warnings: list[str] = []

    # A IA às vezes devolve o ticket como lista (de apostas) em vez de objeto.
    if isinstance(raw, list):
        raw = {"games": raw}
    elif not isinstance(raw, dict):
        raw = {}

    lottery_type = normalize_lottery_type(raw.get("lottery_type"))
    if lottery_type is None:
        warnings.append("Modalidade não reconhecida na leitura — selecione manualmente.")

    draw_number = _to_int(raw.get("draw_number"))
    if draw_number is None:
        warnings.append("Concurso não reconhecido na leitura — informe manualmente.")

    raw_games = raw.get("games") or raw.get("apostas") or []
    if not isinstance(raw_games, list):
        raw_games = []

    games: list[dict[str, Any]] = []
    for gi, raw_game in enumerate(raw_games):
        identifier = chr(ord("A") + gi)
        if isinstance(raw_game, dict) and raw_game.get("identifier"):
            identifier = str(raw_game["identifier"]).strip().upper()[:4] or identifier
        numbers = _game_numbers(raw_game)
        games.append(
            {
                "identifier": identifier,
                "numbers": sorted(numbers),
                "warnings": validate_numbers(numbers, lottery_type),
            }
        )

    if not games:
        warnings.append("Nenhuma aposta foi lida neste comprovante.")

    return {
        "lottery_type": lottery_type,
        "draw_number": draw_number,
        "games": games,
        "warnings": warnings,
        "source_index": index,
    }


def extract_tickets(image_path: str | Path) -> list[dict[str, Any]]:
    """Lê todos os comprovantes de uma imagem.

    Retorno: lista de dicts::

        {
          "lottery_type": "Mega-Sena" | None,
          "draw_number": 3056 | None,
          "games": [{"identifier": "A", "numbers": [...], "warnings": [...]}],
          "warnings": [...],       # avisos no nível do comprovante
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
                    {"type": "image_url", "image_url": {"url": _image_data_url(path)}},
                ],
            }
        ],
        temperature=0.0,
        max_tokens=2048,
    )

    content = response.choices[0].message.content or ""
    try:
        payload = _loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Resposta da IA não é JSON válido: {content[:500]!r}"
        ) from exc

    if isinstance(payload, list):
        raw_tickets = payload
    elif isinstance(payload, dict):
        raw_tickets = payload.get("tickets")
        if raw_tickets is None and ("games" in payload or "apostas" in payload):
            raw_tickets = [payload]  # formato de comprovante único
    else:
        raw_tickets = None

    if not isinstance(raw_tickets, list) or not raw_tickets:
        raise ValueError(f"Nenhum comprovante identificado na leitura: {content[:500]!r}")

    return [_normalize_ticket(t, i) for i, t in enumerate(raw_tickets)]
