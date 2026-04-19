"""Formato legible de instantes guardados en ISO (p. ej. posiciones en portfolio)."""

from __future__ import annotations


def format_utc_datetime_short(iso_str: str | None) -> str:
    """
    Convierte ISO-8601 a 'YYYY-MM-DD HH:MM:SS UTC' para tablas.

    Los timestamps del bot y endDate de Gamma se guardan en UTC; el texto de la
    pregunta del mercado suele estar en hora local de EE.UU. (ET) — por eso no
    coinciden numéricamente con la pregunta si se mezclan zonas.
    """
    if not iso_str:
        return "?"
    s = str(iso_str).strip()
    for sep in ("+", "Z"):
        if sep in s:
            s = s.split(sep, 1)[0].rstrip()
    s = s.replace("T", " ", 1)
    if len(s) >= 19:
        return f"{s[:19]} UTC"
    if len(s) >= 16:
        return f"{s[:16]} UTC"
    return f"{s} UTC" if s else "?"
