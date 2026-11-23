#!/usr/bin/env python3
"""Deterministic discovery script: reads config.toml and generates manifest.json."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.paths import load_config

CID_RX = re.compile(r"\(cid:\d+\)")
HARMLESS_BULLET_PUA = {0xF0B7, 0xF0A7, 0xF0D8, 0xF0FC, 0xF076, 0xF02D, 0xF0A8, 0xF071, 0xF06E}


def analyze_pdf_ocr(pdf_path: Path | None) -> dict[str, Any] | None:
    """Analizza in pochi ms il layer di testo del PDF per capire se serve Forza OCR."""
    if not pdf_path or not pdf_path.exists():
        return None
    try:
        res = subprocess.run(
            ["pdftotext", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
        if res.returncode != 0:
            return {
                "level": "required",
                "label": "OCR necessario",
                "reason": "Impossibile leggere il testo incorporato del PDF",
                "pages": 0,
                "empty_pages": 0,
                "broken_glyphs": 0,
            }
        text = res.stdout
    except Exception:
        return None

    pages = text.split("\x0c")
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    n_pages = max(1, len(pages))
    char_counts = [len(p.strip()) for p in pages]
    total_chars = sum(char_counts)
    avg_chars = total_chars / n_pages
    empty_pages = sum(1 for c in char_counts if c < 20)
    empty_ratio = empty_pages / n_pages

    repl_count = text.count("\ufffd")
    cid_count = len(CID_RX.findall(text))
    broken_pua = sum(
        1
        for ch in text
        if 0xE000 <= ord(ch) <= 0xF8FF and ord(ch) not in HARMLESS_BULLET_PUA
    )
    broken_glyphs = repl_count + cid_count + broken_pua
    broken_ratio = broken_glyphs / max(1, total_chars)

    if avg_chars < 35 or empty_ratio >= 0.50:
        return {
            "level": "required",
            "label": "OCR necessario",
            "reason": f"PDF scansionato o quasi solo immagini ({empty_pages}/{n_pages} pag. senza testo)",
            "pages": n_pages,
            "empty_pages": empty_pages,
            "broken_glyphs": broken_glyphs,
        }
    if repl_count + cid_count >= 10 or broken_ratio >= 0.015:
        return {
            "level": "required",
            "label": "Forza OCR necessario",
            "reason": f"Testo PDF gravemente corrotto ({broken_glyphs} simboli/font rotti su {n_pages} pag.)",
            "pages": n_pages,
            "empty_pages": empty_pages,
            "broken_glyphs": broken_glyphs,
        }
    if empty_ratio >= 0.20 and empty_pages >= 2:
        return {
            "level": "recommended",
            "label": "OCR consigliato",
            "reason": f"Molte slide solo immagine ({empty_pages}/{n_pages} pag. senza testo)",
            "pages": n_pages,
            "empty_pages": empty_pages,
            "broken_glyphs": broken_glyphs,
        }
    if broken_glyphs >= 12:
        return {
            "level": "recommended",
            "label": "Forza OCR consigliato",
            "reason": f"Font/formule non standard nel PDF ({broken_glyphs} glifi matematici non decodificati)",
            "pages": n_pages,
            "empty_pages": empty_pages,
            "broken_glyphs": broken_glyphs,
        }
    return {
        "level": "ok",
        "label": "Testo digitale OK",
        "reason": f"Testo pulito ({n_pages} pag., auto OK)",
        "pages": n_pages,
        "empty_pages": empty_pages,
        "broken_glyphs": broken_glyphs,
    }


def part_sort_key(part_id: str) -> tuple[int, str]:
    m = re.match(r"^(\d+)([a-zA-Z]*)$", part_id)
    if m:
        return (int(m.group(1)), m.group(2).lower())
    return (999999, part_id.lower())


def slugify_title(title: str, chapter_num: int) -> str:
    if not title:
        return f"cap{chapter_num}"
    # Rimuove eventuali parentesi es. "Viti e bulloni (Collegamenti filettati)" -> "viti e bulloni"
    clean = re.sub(r"\s*\([^)]*\)", "", title).strip().lower()
    clean = re.sub(r"[/\\:*?\"<>|]+", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or f"cap{chapter_num}"


def is_git_repo(path: Path, git_bin: str) -> bool:
    if not path.exists():
        return False
    res = subprocess.run(
        [git_bin, "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    return res.returncode == 0 and res.stdout.strip() == "true"


def find_chapter_dir(source_dir: Path, chapter_num: int, pattern: str) -> tuple[Path | None, str]:
    rx = re.compile(pattern)
    for entry in sorted(source_dir.iterdir()):
        if not entry.is_dir():
            continue
        m = rx.match(entry.name)
        if m and int(m.group(1)) == chapter_num:
            title = m.group(2).strip() if m.lastindex and m.lastindex >= 2 else entry.name
            return entry, title
    return None, ""


def scan_subfolders(
    cfg_mat: dict[str, Any], chapter_num: int
) -> tuple[Path | None, str, list[dict[str, Any]]]:
    source_dir = Path(cfg_mat["source_dir"]).expanduser()
    chapter_dir, title = find_chapter_dir(
        source_dir, chapter_num, cfg_mat["chapter_dir_pattern"]
    )
    if chapter_dir is None:
        return None, "", []

    pdf_rx = re.compile(cfg_mat["pdf_pattern"])
    txt_rx = re.compile(cfg_mat["txt_pattern"])
    media_rx = re.compile(cfg_mat["media_pattern"])

    pdfs: dict[str, Path] = {}
    txts: dict[str, Path] = {}
    medias: dict[str, Path] = {}

    def collect_from_dir(d: Path, include_pdfs: bool) -> None:
        if not d.exists():
            return
        for f in sorted(d.iterdir()):
            if not f.is_file():
                continue
            if include_pdfs:
                m = pdf_rx.match(f.name)
                if m and int(m.group(1)) == chapter_num:
                    pdfs.setdefault(m.group(2).lower(), f)
                    continue
            m = txt_rx.match(f.name)
            if m and int(m.group(1)) == chapter_num:
                txts.setdefault(m.group(2).lower(), f)
                continue
            m = media_rx.match(f.name)
            if m and int(m.group(1)) == chapter_num:
                medias.setdefault(m.group(2).lower(), f)

    collect_from_dir(chapter_dir, include_pdfs=True)
    for extra in cfg_mat.get("extra_media_dirs", []):
        collect_from_dir(Path(extra).expanduser(), include_pdfs=False)

    # Fallback match: e.g. ECM01_9a.txt -> ECM01-9.pdf if "9a" has no own PDF
    for dct in (txts, medias):
        for k in list(dct.keys()):
            if k not in pdfs:
                base = re.sub(r"[a-z]+$", "", k)
                if base and base in pdfs and base not in dct:
                    dct[base] = dct.pop(k)

    all_parts = sorted(set(pdfs) | set(txts) | set(medias), key=part_sort_key)
    parts: list[dict[str, Any]] = []
    for pid in all_parts:
        pdf = pdfs.get(pid)
        txt = txts.get(pid)
        media = medias.get(pid)
        if pdf and txt:
            status = "ready"
        elif pdf and media and not txt:
            status = "needs_transcription"
        elif pdf:
            status = "pdf_only"
        else:
            status = "missing_pdf"
        parts.append(
            {
                "part_id": pid,
                "pdf": str(pdf) if pdf else None,
                "txt": str(txt) if txt else None,
                "media": str(media) if media else None,
                "needs_transcription": bool(media and not txt),
                "status": status,
                "ocr_info": analyze_pdf_ocr(pdf),
            }
        )
    return chapter_dir, title, parts


def scan_flat_files(
    cfg_mat: dict[str, Any], chapter_num: int
) -> tuple[Path, str, list[dict[str, Any]]]:
    source_dir = Path(cfg_mat["source_dir"]).expanduser()
    pdf_rx = re.compile(cfg_mat["pdf_pattern"])
    txt_rx = re.compile(cfg_mat["txt_pattern"])
    media_rx = re.compile(cfg_mat["media_pattern"])

    pdfs: list[Path] = []
    txts: list[Path] = []
    medias: list[Path] = []

    for f in sorted(source_dir.iterdir()):
        if not f.is_file():
            continue
        m = pdf_rx.match(f.name)
        if m and int(m.group(1)) == chapter_num:
            pdfs.append(f)
            continue
        m = txt_rx.match(f.name)
        if m and int(m.group(1)) == chapter_num:
            txts.append(f)
            continue
        m = media_rx.match(f.name)
        if m and int(m.group(1)) == chapter_num:
            medias.append(f)

    count = max(len(pdfs), len(txts), len(medias))
    parts: list[dict[str, Any]] = []
    for idx in range(count):
        pdf = pdfs[idx] if idx < len(pdfs) else None
        txt = txts[idx] if idx < len(txts) else None
        media = medias[idx] if idx < len(medias) else None
        if pdf and txt:
            status = "ready"
        elif pdf and media and not txt:
            status = "needs_transcription"
        elif pdf:
            status = "pdf_only"
        else:
            status = "missing_pdf"
        parts.append(
            {
                "part_id": str(idx + 1),
                "pdf": str(pdf) if pdf else None,
                "txt": str(txt) if txt else None,
                "media": str(media) if media else None,
                "needs_transcription": bool(media and not txt),
                "status": status,
                "ocr_info": analyze_pdf_ocr(pdf),
            }
        )
    return source_dir, f"Modulo {chapter_num}", parts


def build_manifest(config_path: Path | None, materia: str, chapter_num: int) -> dict[str, Any]:
    resolved_cfg, cfg = load_config(config_path)

    general = cfg["general"]
    materie = cfg["materie"]
    if materia not in materie:
        raise SystemExit(f"Errore: materia '{materia}' non trovata in {resolved_cfg}")

    cfg_mat = materie[materia]
    layout = cfg_mat.get("chapter_layout", "subfolders")
    if layout == "subfolders":
        chapter_dir, title, parts = scan_subfolders(cfg_mat, chapter_num)
    elif layout == "flat_files":
        chapter_dir, title, parts = scan_flat_files(cfg_mat, chapter_num)
    else:
        raise SystemExit(f"Errore: chapter_layout sconosciuto '{layout}'")

    typst_dir = Path(cfg_mat["typst_project_dir"]).expanduser()
    missing = {
        "chapter_not_found": chapter_dir is None,
        "no_pdfs": not any(p["pdf"] for p in parts),
        "parts_without_txt": [p["part_id"] for p in parts if p["pdf"] and not p["txt"]],
        "parts_needing_transcription": [
            p["part_id"] for p in parts if p["needs_transcription"]
        ],
        "parts_without_pdf": [p["part_id"] for p in parts if not p["pdf"]],
    }

    return {
        "materia": materia,
        "materia_name": cfg_mat["name"],
        "capitolo": chapter_num,
        "chapter_title": title,
        "chapter_slug": slugify_title(title, chapter_num),
        "source_dir": str(Path(cfg_mat["source_dir"]).expanduser()),
        "chapter_dir": str(chapter_dir) if chapter_dir else None,
        "shared_source_dir": str(Path(cfg_mat["shared_source_dir"]).expanduser()) if cfg_mat.get("shared_source_dir") else "",
        "typst_project_dir": str(typst_dir),
        "main_file": cfg_mat["main_file"],
        "chapters_dir": cfg_mat["chapters_dir"],
        "figs_dir": cfg_mat["figs_dir"],
        "max_fig_height": cfg_mat.get(
            "max_fig_height", general["default_max_fig_height"]
        ),
        "is_git_repo": is_git_repo(typst_dir, general["git_bin"]),
        "parts": parts,
        "missing": missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Discovery deterministico per Appunti")
    parser.add_argument("materia", help="Chiave materia in config.toml (es. ecm)")
    parser.add_argument("capitolo", type=int, help="Numero capitolo (es. 12)")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    manifest = build_manifest(args.config, args.materia.lower(), args.capitolo)

    _, cfg = load_config(args.config)
    work_dir = Path(cfg["general"]["work_dir"]).expanduser()
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out or (work_dir / f"{args.materia.lower()}_cap{args.capitolo}_manifest.json")
    out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Manifest scritto in: {out_path}")
    print(
        f"Capitolo {manifest['capitolo']} ({manifest['chapter_title'] or 'NON TROVATO'}): "
        f"{len(manifest['parts'])} parti trovate, "
        f"senza .txt: {manifest['missing']['parts_without_txt']}, "
        f"da trascrivere: {manifest['missing']['parts_needing_transcription']}"
    )
    if manifest["missing"]["chapter_not_found"] or manifest["missing"]["no_pdfs"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
