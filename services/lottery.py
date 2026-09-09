"""Regras das modalidades e normalização/validação de dezenas."""
from __future__ import annotations

import re

LOTTERY_TYPES: tuple[str, ...] = ("Mega-Sena", "Quina")

# min_n/max_n: faixa válida das dezenas.
# min_count/max_count: quantidade de dezenas por aposta (aposta simples ... máximo).
LOTTERY_RULES: dict[str, dict[str, int]] = {
    "Mega-Sena": {"min_n": 1, "max_n": 60, "min_count": 6, "max_count": 20},
    "Quina": {"min_n": 1, "max_n": 80, "min_count": 5, "max_count": 15},
}


def normalize_lottery_type(value: str | None) -> str | None:
    """Devolve 'Mega-Sena', 'Quina' ou None."""
    if not value:
        return None
    v = str(value).strip().lower()
    if "mega" in v:
        return "Mega-Sena"
    if "quina" in v:
        return "Quina"
    return None


def clean_numbers(raw) -> list[int]:
    """Coage uma lista heterogênea em inteiros, removendo lixo e duplicatas.

    Aceita ints, strings ('04', '5a', ' 12 ') ou já uma string separada por
    qualquer não-dígito. Mantém a ordem de aparição.
    """
    if isinstance(raw, str):
        raw = re.split(r"[^\d]+", raw)

    out: list[int] = []
    seen: set[int] = set()
    for item in raw or []:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            n = item
        else:
            match = re.search(r"\d+", str(item))
            if not match:
                continue
            n = int(match.group())
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def validate_numbers(numbers: list[int], lottery_type: str | None) -> list[str]:
    """Lista de avisos (strings) — vazia significa aposta consistente."""
    warnings: list[str] = []
    rules = LOTTERY_RULES.get(lottery_type or "")
    if rules is None:
        return warnings  # sem modalidade não há como validar a faixa

    fora = [n for n in numbers if n < rules["min_n"] or n > rules["max_n"]]
    if fora:
        warnings.append(
            f"Dezenas fora da faixa {rules['min_n']}–{rules['max_n']}: "
            + ", ".join(map(str, fora))
        )
    if len(numbers) < rules["min_count"]:
        warnings.append(
            f"Só {len(numbers)} dezena(s); o mínimo da modalidade é "
            f"{rules['min_count']}."
        )
    elif len(numbers) > rules["max_count"]:
        warnings.append(
            f"{len(numbers)} dezenas; o máximo da modalidade é "
            f"{rules['max_count']}."
        )
    return warnings
