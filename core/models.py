"""Registro centrale dei modelli Gemini supportati da Appunti Typst Studio."""

from __future__ import annotations

# Modelli selezionabili via agy CLI (in ordine consigliato per qualita/velocita)
AGY_GEMINI_MODELS: list[tuple[str, str]] = [
    ("gemini-4-hydrogen-high", "Gemini 4 Hydrogen (High) — Consigliato CeTZ Studio"),
    ("gemini-4-hydrogen-medium", "Gemini 4 Hydrogen (Medium) — Bilanciato (default pipeline)"),
    ("gemini-4-hydrogen-low", "Gemini 4 Hydrogen (Low) — Veloce"),
    ("gemini-4-hydrogen-xhigh", "Gemini 4 Hydrogen (XHigh) — Massimo ragionamento"),
    ("gemini-4-neon-high", "Gemini 4 Neon (High)"),
    ("gemini-4-neon-medium", "Gemini 4 Neon (Medium)"),
    ("gemini-4-neon-low", "Gemini 4 Neon (Low)"),
    ("gemini-4-neon-xhigh", "Gemini 4 Neon (XHigh)"),
    ("gemini-4-argon-high", "Gemini 4 Argon (High)"),
    ("gemini-4-argon-medium", "Gemini 4 Argon (Medium)"),
    ("gemini-4-argon-low", "Gemini 4 Argon (Low)"),
    ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High) — Economico"),
    ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium) — Economico veloce"),
    ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low) — Ultra veloce"),
]

# Modelli selezionabili via Gemini REST API diretta (usando GEMINI_API_KEY / gemini.key)
GEMINI_API_MODELS: list[tuple[str, str]] = [
    ("gemini-2.5-pro", "Gemini 2.5 Pro — Alta qualità (REST API)"),
    ("gemini-2.5-flash", "Gemini 2.5 Flash — Veloce e bilanciato (REST API)"),
    ("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite — Ultra veloce (REST API)"),
]

AGY_MODEL_IDS: list[str] = [m[0] for m in AGY_GEMINI_MODELS]
GEMINI_API_MODEL_IDS: list[str] = [m[0] for m in GEMINI_API_MODELS]

DEFAULT_MODELS: dict[str, str] = {
    "model_notes": "gemini-4-hydrogen-medium",
    "model_figures": "gemini-4-hydrogen-medium",
    "model_cetz_studio": "gemini-4-hydrogen-high",
    "model_cetz_screenshot": "gemini-4-hydrogen-medium",
    "model_transcribe": "gemini-4-hydrogen-medium",
    "gemini_transcribe_model": "gemini-3.5-transcribe",
    "gemini_api_model": "gemini-2.5-pro",
    "gemini_api_fast_model": "gemini-2.5-flash",
}


def resolve_api_model(requested: str, fallback: str = "gemini-2.5-pro") -> str:
    """Se il modello richiesto è un ID solo-agy, mappa al modello REST API configurato."""
    if requested in GEMINI_API_MODEL_IDS or requested.startswith(("gemini-2.", "gemini-1.", "gemini-3.5")):
        return requested
    if "flash" in requested.lower() or "low" in requested.lower():
        return "gemini-2.5-flash"
    return fallback
