"""
Cross-market arbitrage detection for weather markets.

Polymarket a veces lista múltiples bins mutuamente excluyentes para la misma
ciudad + fecha (p.ej. "21°C", "22°C", "23°C" todos como temperature exact).
Cuando la suma de YES-prices de esos bins > 1.0, existe un overround
estructural: al menos uno de los bins está sobrevalorado y el NO
correspondiente tiene edge independiente del forecast.

Este módulo agrupa los mercados por (ciudad, fecha) y devuelve el set de
condition_ids que pertenecen a un grupo arbitrable (suma YES > threshold).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Optional

from loguru import logger

from bots.weather.parser import WeatherMarketParser
from core.models import Market, WeatherCondition


def _reference_date(market: Market) -> date:
    ed = market.end_date
    if ed.tzinfo is not None:
        return ed.astimezone(ed.tzinfo).date()
    return ed.date()


def detect_arbitrage_cids(
    markets: list[Market],
    parser: WeatherMarketParser,
    overround_threshold: float = 1.05,
) -> set[str]:
    """
    Agrupa los markets exact-temperature por (ciudad, fecha_objetivo) y devuelve
    el set de condition_ids pertenecientes a grupos con overround > threshold.

    Solo considera TEMPERATURE_EXACT (mutuamente excluyentes por definición).
    Para otras condiciones (rain, temp_above) los límites no son disjoint y
    el análisis estructural es más ruidoso.
    """
    groups: dict[tuple[str, date], list[Market]] = defaultdict(list)

    for m in markets:
        try:
            info = parser.parse(m.condition_id, m.question, reference_date=_reference_date(m))
        except Exception:
            continue
        if info is None or info.condition != WeatherCondition.TEMPERATURE_EXACT:
            continue
        key = (info.location.lower(), info.target_date)
        groups[key].append(m)

    arb_cids: set[str] = set()
    for (city, tdate), group in groups.items():
        if len(group) < 2:
            continue
        yes_sum = sum(m.yes_price for m in group)
        if yes_sum > overround_threshold:
            logger.info(
                f"[ARB] {city} {tdate}: {len(group)} bins, sum(YES)={yes_sum:.2%} "
                f"→ overround {(yes_sum-1)*100:+.1f}pp → NO-side arbitrage edge"
            )
            for m in group:
                arb_cids.add(m.condition_id)

    return arb_cids
