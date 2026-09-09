"""Lógica de cruzamento de apostas vs. sorteio."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

NO_PRIZE = "Sem Premiação"

# Faixas premiadas por modalidade: {qtd_acertos: nome_da_faixa}
PRIZE_TIERS: dict[str, dict[int, str]] = {
    "Mega-Sena": {6: "Sena", 5: "Quina", 4: "Quadra"},
    "Quina": {5: "Quina", 4: "Quadra", 3: "Terno", 2: "Duque"},
}


def evaluate(
    numbers: Iterable[int],
    drawn_numbers: Iterable[int],
    lottery_type: str,
) -> dict[str, Any]:
    """Compara um jogo com o sorteio e classifica a premiação.

    Retorno::

        {
            "hits": [10, 24, 40],          # dezenas acertadas (ordenadas)
            "hit_count": 3,
            "prize": "Terno" | "Sem Premiação",
            "is_winner": bool,
        }
    """
    played = set(numbers)
    drawn = set(drawn_numbers)
    hits = sorted(played & drawn)

    tier = PRIZE_TIERS.get(lottery_type, {}).get(len(hits))
    return {
        "hits": hits,
        "hit_count": len(hits),
        "prize": tier or NO_PRIZE,
        "is_winner": tier is not None,
    }
