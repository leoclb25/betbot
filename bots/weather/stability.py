"""
Forecast stability guard.

Requiere que la probabilidad estimada (true_prob) de un mercado se mantenga
estable entre dos scans consecutivos antes de permitir una entrada. Si entre
el scan anterior y el actual la prob cambió más de `max_delta`, el mercado
queda en observación un ciclo más.

Motivación: entrar en la primera aparición del mercado significa operar sobre
un único run del ensemble. Un segundo run con forecast similar es evidencia de
estabilidad del sistema atmosférico; uno con forecast distinto es advertencia
de que estamos frente a un caso de alta incertidumbre donde el edge estimado
no es confiable.

Persiste el estado en data/weather_forecast_cache.json (tolerante a reinicios).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

CACHE_FILE = Path("data/weather_forecast_cache.json")
# Entradas más viejas que esto se consideran "baseline perdida" y cuentan como primer scan.
OBSERVATION_TTL_HOURS = 24


class ForecastStabilityGuard:
    """
    Mantiene un cache por condition_id con la última true_prob estimada.
    Permite entrar solo si la prob actual está dentro de `max_delta` de la anterior
    observación reciente.
    """

    def __init__(
        self,
        max_delta: float = 0.05,
        required_observations: int = 2,
        enabled: bool = True,
    ) -> None:
        self._max_delta = max_delta
        self._required_observations = max(1, required_observations)
        self._enabled = enabled
        self._cache: dict[str, dict] = self._load()

    # ── I/O ──────────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if not CACHE_FILE.exists():
            return {}
        try:
            with CACHE_FILE.open() as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with CACHE_FILE.open("w") as f:
                json.dump(self._cache, f)
        except OSError as exc:
            logger.debug(f"[STABILITY] save error: {exc}")

    def _prune_expired(self) -> None:
        now = datetime.now(timezone.utc)
        stale: list[str] = []
        for cid, entry in self._cache.items():
            try:
                last_ts = datetime.fromisoformat(entry["updated_at"])
            except (KeyError, ValueError):
                stale.append(cid)
                continue
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            if now - last_ts > timedelta(hours=OBSERVATION_TTL_HOURS):
                stale.append(cid)
        for cid in stale:
            self._cache.pop(cid, None)

    # ── Public API ───────────────────────────────────────────────────────────

    def observe(self, condition_id: str, true_prob: float) -> tuple[bool, str]:
        """
        Registra la observación actual y decide si la entrada está permitida.
        Returns (is_stable, reason).

        Primera vez que se ve este condition_id → is_stable=False (hay que esperar).
        Segunda vez con prob similar → is_stable=True.
        Si cambió > max_delta → resetea el contador y vuelve a esperar.
        """
        if not self._enabled:
            return True, "stability guard disabled"

        self._prune_expired()
        now = datetime.now(timezone.utc)
        prev = self._cache.get(condition_id)

        if prev is None:
            self._cache[condition_id] = {
                "last_prob": true_prob,
                "updated_at": now.isoformat(),
                "observations": 1,
            }
            self._save()
            return False, "first observation — awaiting confirmation next cycle"

        last_prob = float(prev.get("last_prob", true_prob))
        observations = int(prev.get("observations", 1))
        delta = abs(true_prob - last_prob)

        if delta > self._max_delta:
            # Reset: forecast cambió demasiado, volvemos a observar
            self._cache[condition_id] = {
                "last_prob": true_prob,
                "updated_at": now.isoformat(),
                "observations": 1,
            }
            self._save()
            return False, (
                f"forecast unstable (Δ={delta:.1%} > {self._max_delta:.1%}) — resetting observation"
            )

        observations += 1
        self._cache[condition_id] = {
            "last_prob": true_prob,
            "updated_at": now.isoformat(),
            "observations": observations,
        }
        self._save()

        if observations < self._required_observations:
            return False, (
                f"awaiting {self._required_observations - observations} more stable observations"
            )

        return True, f"stable for {observations} observations (Δ={delta:.1%})"

    def clear(self, condition_id: str) -> None:
        """Elimina el tracking de un mercado (ej: al abrir posición)."""
        if condition_id in self._cache:
            del self._cache[condition_id]
            self._save()
