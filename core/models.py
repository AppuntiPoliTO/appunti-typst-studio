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

AGY_MODEL_IDS: list[str] = [m[0] for m in AGY_GEMINI_MODELS]

DEFAULT_MODELS: dict[str, str] = {
    "model_notes": "gemini-4-hydrogen-medium",
    "model_figures": "gemini-4-hydrogen-medium",
    "model_cetz_studio": "gemini-4-hydrogen-high",
    "model_cetz_screenshot": "gemini-4-hydrogen-medium",
    "model_transcribe": "gemini-4-hydrogen-medium",
    "gemini_transcribe_model": "gemini-3.5-transcribe",
}
