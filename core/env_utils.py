"""
Helpers para leer variables de entorno de forma tolerante.

Motivación: systemd's `EnvironmentFile=` NO strippea comentarios inline
(a diferencia de python-dotenv). Un `.env` con `KEY=0.05  # note` hace que
`os.getenv("KEY")` devuelva `'0.05  # note'` y rompa int()/float().

Uso:
    from core.env_utils import env_float, env_int
    val = env_float("MIN_EDGE", 0.08)
"""

from __future__ import annotations

import os


def _strip_inline_comment(raw: str) -> str:
    if "#" in raw:
        raw = raw.split("#", 1)[0]
    return raw.strip()


def env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return float(_strip_inline_comment(raw))
    except ValueError:
        return default


def env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(_strip_inline_comment(raw))
    except ValueError:
        return default


def env_str(key: str, default: str) -> str:
    raw = os.getenv(key)
    if raw is None:
        return default
    return _strip_inline_comment(raw) or default
