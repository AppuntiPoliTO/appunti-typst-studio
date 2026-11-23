"""Risoluzione portabile di percorsi, configurazione e binari (zero path utente hardcoded)."""

from __future__ import annotations

import os
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
    gen.setdefault("default_max_fig_height", "6cm")
    gen.setdefault("audio_chunk_seconds", 600)
    gen.setdefault("audio_overlap_seconds", 8)
    gen.setdefault("figure_max_retries", 2)
    gen.setdefault("figure_batch_size", 5)

    return cfg_path, cfg
