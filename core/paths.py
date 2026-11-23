"""Risoluzione portabile di percorsi, configurazione e binari (zero path utente hardcoded)."""

from __future__ import annotations

import os
import re
import shutil
import tomllib
from pathlib import Path
from typing import Any

from .models import DEFAULT_MODELS

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
TEMPLATES_DIR = REPO_ROOT / "templates"
ASSETS_DIR = REPO_ROOT / "assets"
BIN_DIR = REPO_ROOT / "bin"


def find_config_path(explicit: Path | None = None) -> Path:
    if explicit and explicit.exists():
        return explicit.expanduser().resolve()
    env_cfg = os.environ.get("APPUNTI_CONFIG", "").strip()
    if env_cfg and Path(env_cfg).expanduser().exists():
        return Path(env_cfg).expanduser().resolve()

    candidates = [
        REPO_ROOT / "config.toml",
        Path.home() / ".config/appunti-typst-studio/config.toml",
        Path.home() / "Documents/Appunti/config.toml",
        REPO_ROOT / "config.example.toml",
    ]
    for cand in candidates:
        if cand.exists():
            return cand.resolve()
    return REPO_ROOT / "config.toml"


def resolve_bin(configured: str | None, name: str, extra_candidates: list[Path] | None = None) -> str:
    if configured:
        p = Path(configured).expanduser()
        if p.exists():
            return str(p)
        found = shutil.which(configured)
        if found:
            return found

    for cand in extra_candidates or []:
        c = cand.expanduser()
        if c.exists():
            return str(c)

    found = shutil.which(name)
    if found:
        return found

    local_bin = Path.home() / ".local/bin" / name
    if local_bin.exists():
        return str(local_bin)

    return configured or name


def load_gemini_api_key(general: dict[str, Any]) -> str | None:
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key
    key_file_cfg = general.get("gemini_api_key_file", "~/.config/appunti/gemini.key")
    if key_file_cfg:
        kf = Path(key_file_cfg).expanduser()
        if kf.exists():
            val = kf.read_text(encoding="utf-8").strip()
            if val:
                return val
    return None


def load_config(explicit_path: Path | None = None) -> tuple[Path, dict[str, Any]]:
    cfg_path = find_config_path(explicit_path)
    with cfg_path.open("rb") as fh:
        cfg = tomllib.load(fh)

    gen = cfg.setdefault("general", {})
    for k, v in DEFAULT_MODELS.items():
        gen.setdefault(k, v)

    gen["prompts_dir"] = str(Path(gen.get("prompts_dir") or PROMPTS_DIR).expanduser())
    gen["work_dir"] = str(Path(gen.get("work_dir") or (REPO_ROOT / "work")).expanduser())
    gen["cetz_backup_dir"] = str(
        Path(gen.get("cetz_backup_dir") or (Path.home() / "Documents/CeTZ_Backup")).expanduser()
    )
    gen["screenshots_dir"] = str(
        Path(gen.get("screenshots_dir") or (Path.home() / "Screenshots")).expanduser()
    )
    gen["agy_bin"] = resolve_bin(gen.get("agy_bin"), "agy", [Path.home() / ".local/bin/agy"])
    gen["typst_bin"] = resolve_bin(gen.get("typst_bin"), "typst", [Path.home() / ".local/bin/typst"])
    gen["ffmpeg_bin"] = resolve_bin(gen.get("ffmpeg_bin"), "ffmpeg")
    gen["pdftoppm_bin"] = resolve_bin(gen.get("pdftoppm_bin"), "pdftoppm")
    gen["git_bin"] = resolve_bin(gen.get("git_bin"), "git")
    gen["mineru_bin"] = resolve_bin(
        gen.get("mineru_bin"),
        "mineru-kit",
        [
            REPO_ROOT / ".venv-mineru/bin/mineru-kit",
            Path.home() / "Documents/Appunti/.venv-mineru/bin/mineru-kit",
            Path.home() / ".local/bin/mineru-kit",
        ],
    )
    gen.setdefault("mineru_tier", "basic")
    gen.setdefault("use_mineru_default", True)
    gen.setdefault("llm_backend", "agy")
    gen.setdefault("figures_mode", "hybrid")
    gen.setdefault("gemini_api_key_file", "~/.config/appunti/gemini.key")
    gen.setdefault("default_max_fig_height", "6cm")
    gen.setdefault("audio_chunk_seconds", 600)
    gen.setdefault("audio_overlap_seconds", 8)
    gen.setdefault("figure_max_retries", 2)
    gen.setdefault("figure_batch_size", 5)

    return cfg_path, cfg


def append_subject_to_config(
    cfg_path: Path,
    *,
    key: str,
    name: str,
    source_dir: str,
    typst_project_dir: str,
    layout: str = "subfolders",
    shared_source_dir: str = "",
) -> None:
    """Aggiunge in modo sicuro una nuova sezione [materie.<key>] in config.toml."""
    safe_key = re.sub(r"[^a-z0-9_]+", "_", key.strip().lower()).strip("_")
    if not safe_key:
        raise ValueError("ID materia non valido")

    if layout == "subfolders":
        chap_pat = r"'^(\d{1,2})\s*-\s*(.+)$'"
        prefix = re.escape(safe_key)
        pdf_pat = f"'(?i)^(?:{prefix}\\s*)?(\\d+)[-_](\\d+[a-z]?)(?:\\s+(.*))?\\.pdf$'"
        txt_pat = f"'(?i)^(?:{prefix}\\s*)?(\\d+)[-_](\\d+[a-z]?)(?:\\s*\\(.*\\))?\\.txt$'"
        med_pat = f"'(?i)^(?:{prefix}\\s*)?(\\d+)[-_](\\d+[a-z]?)(?:\\s*.*)?\\.(mp4|mkv|webm|mov|avi|mp3|wav|m4a|ogg)$'"
    else:
        chap_pat = "''"
        pdf_pat = r"'(?i)^(?:modulo|cap|lez(?:ione)?)\s*(\d+)(?:[_\s]+(.*))?\.pdf$'"
        txt_pat = r"'(?i)^(?:modulo|cap|lez(?:ione)?)\s*(\d+)(?:[_\s]+(.*))?\.txt$'"
        med_pat = r"'(?i)^(?:modulo|cap|lez(?:ione)?)\s*(\d+)(?:[_\s]+.*)?\.(mp4|mkv|webm|mov|avi|mp3|wav|m4a|ogg)$'"

    def esc(val: str) -> str:
        return val.replace("\\", "\\\\").replace('"', '\\"')

    block = (
        f"\n[materie.{safe_key}]\n"
        f'name = "{esc(name.strip())}"\n'
        f'source_dir = "{esc(source_dir.strip())}"\n'
        f'chapter_layout = "{layout}"\n'
        f"chapter_dir_pattern = {chap_pat}\n"
        f'shared_source_dir = "{esc(shared_source_dir.strip())}"\n'
        f'typst_project_dir = "{esc(typst_project_dir.strip())}"\n'
        f'main_file = "main.typ"\n'
        f'chapters_dir = "."\n'
        f'figs_dir = "figs"\n'
        f'max_fig_height = "6cm"\n'
        f"pdf_pattern = {pdf_pat}\n"
        f"txt_pattern = {txt_pat}\n"
        f"media_pattern = {med_pat}\n"
        f"extra_media_dirs = []\n"
    )
    existing = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    cfg_path.write_text(existing + block, encoding="utf-8")
