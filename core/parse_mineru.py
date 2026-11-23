#!/usr/bin/env python3
"""Parsing PDF locale tramite MinerU 4.x (ONNX CPU): estrae Markdown OCR, formule e ritagli figure.

Mantiene una cache in work/mineru_cache/<materia>_cap<N>/p<part_id>/ basata su mtime+size del PDF,
così un PDF già analizzato non viene mai riparsato a meno che non si passi --force.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.paths import load_config

IMG_NAME_PAGE_RX = re.compile(r"^page_(\d+)_", re.IGNORECASE)


def emit_progress(**payload: Any) -> None:
    print(f"@@PROGRESS {json.dumps(payload, ensure_ascii=False)}", flush=True)


def pdf_fingerprint(pdf_path: Path, ocr_mode: str = "auto") -> dict[str, Any]:
    st = pdf_path.stat()
    return {
        "source_pdf": str(pdf_path.resolve()),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ocr_mode": ocr_mode,
    }


def is_cache_valid(part_cache_dir: Path, pdf_path: Path, ocr_mode: str = "auto") -> bool:
    meta_path = part_cache_dir / "meta.json"
    md_path = part_cache_dir / "markdown.md"
    idx_path = part_cache_dir / "index.json"
    if not (meta_path.exists() and md_path.exists() and idx_path.exists()):
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta == pdf_fingerprint(pdf_path, ocr_mode=ocr_mode)
    except Exception:
        return False


def extract_zip_to_cache(
    zip_path: Path,
    part_cache_dir: Path,
    pdf_path: Path,
    ocr_mode: str = "auto",
) -> dict[str, Any]:
    if part_cache_dir.exists():
        shutil.rmtree(part_cache_dir)
    part_cache_dir.mkdir(parents=True, exist_ok=True)
    images_dir = part_cache_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(part_cache_dir)

    # Costruisci indice pulito delle immagini ritagliate (escludendo ritagli equation_* già OCR'd)
    images: list[dict[str, Any]] = []
    if images_dir.exists():
        for img in sorted(images_dir.iterdir()):
            if not img.is_file():
                continue
            if "_equation_" in img.name.lower():
                continue
            m = IMG_NAME_PAGE_RX.match(img.name)
            page_1based = int(m.group(1)) + 1 if m else 1
            images.append(
                {
                    "filename": img.name,
                    "rel_path": f"images/{img.name}",
                    "abs_path": str(img.resolve()),
                    "page": page_1based,
                }
            )

    md_path = part_cache_dir / "markdown.md"
    index_data = {
        "source_pdf": str(pdf_path),
        "markdown": str(md_path.resolve()),
        "images_dir": str(images_dir.resolve()),
        "images": images,
    }
    (part_cache_dir / "index.json").write_text(
        json.dumps(index_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (part_cache_dir / "meta.json").write_text(
        json.dumps(pdf_fingerprint(pdf_path, ocr_mode=ocr_mode), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return index_data


def main() -> int:
    parser = argparse.ArgumentParser(description="Parsing PDF con MinerU ONNX + cache locale")
    parser.add_argument("manifest", type=Path, help="Percorso al file manifest.json")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--parts", default="", help="Lista parti separate da virgola (es. 1,2)")
    parser.add_argument("--force", action="store_true", help="Ignora cache e riesegui MinerU")
    parser.add_argument("--force-ocr", action="store_true", help="Forza --ocr-mode ocr su tutte le pagine")
    args = parser.parse_args()

    _, cfg = load_config(args.config)
    general = cfg["general"]
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))

    mineru_bin = Path(general["mineru_bin"]).expanduser()
    if not mineru_bin.exists():
        print(f"Errore: mineru-kit non trovato in {mineru_bin}. Esegui ./install.sh --with-mineru", file=sys.stderr, flush=True)
        return 1
    tier = general.get("mineru_tier", "basic")
    ocr_mode = "ocr" if args.force_ocr else "auto"

    allowed = {x.strip().lower() for x in args.parts.split(",") if x.strip()} if args.parts else None
    target_parts = [
        p
        for p in manifest["parts"]
        if p.get("pdf") and (allowed is None or p["part_id"].lower() in allowed)
    ]

    work_dir = Path(general["work_dir"]).expanduser()
    cache_root = work_dir / "mineru_cache" / f"{manifest['materia']}_cap{manifest['capitolo']}"
    cache_root.mkdir(parents=True, exist_ok=True)

    to_parse: list[tuple[dict[str, Any], Path, Path]] = []
    cached: list[tuple[dict[str, Any], Path]] = []
    for part in target_parts:
        pdf_path = Path(part["pdf"])
        part_cache = cache_root / f"p{part['part_id']}"
        if not args.force and is_cache_valid(part_cache, pdf_path, ocr_mode=ocr_mode):
            cached.append((part, part_cache))
        else:
            to_parse.append((part, pdf_path, part_cache))

    emit_progress(
        phase="mineru",
        status="plan",
        done=0,
        total=len(target_parts),
        to_parse=len(to_parse),
    )

    done_count = 0
    for part, part_cache in cached:
        done_count += 1
        idx = json.loads((part_cache / "index.json").read_text(encoding="utf-8"))
        print(
            f"  ✓ Cache MinerU valida per Parte {part['part_id']} "
            f"({len(idx.get('images', []))} figure ritagliate)",
            flush=True,
        )
        emit_progress(
            phase="mineru",
            status="item_skip",
            done=done_count,
            total=len(target_parts),
            item=f"Parte {part['part_id']} (cache)",
        )

    if to_parse:
        # Esegue MinerU una parte alla volta (o batch se >1) — in batch carica i modelli ONNX 1 sola volta!
        # Nota: per mostrare progresso per parte manteniamo un batch singolo se ci sono più parti con nomi univoci.
        with tempfile.TemporaryDirectory(prefix="appunti_mineru_") as tmp:
            tmp_dir = Path(tmp)
            # Copiamo/linkiamo i PDF con nomi univoci (p<id>.pdf) per associare con certezza ogni .zip alla parte
            staged_inputs: list[tuple[dict[str, Any], Path, Path, Path]] = []
            for part, pdf_path, part_cache in to_parse:
                link_pdf = tmp_dir / f"p{part['part_id']}.pdf"
                link_pdf.symlink_to(pdf_path.resolve())
                staged_inputs.append((part, pdf_path, part_cache, link_pdf))

            out_dir = tmp_dir / "out"
            out_dir.mkdir()

            t0 = time.monotonic()
            part_names = ", ".join(f"Parte {p['part_id']}" for p, _, _, _ in staged_inputs)
            emit_progress(
                phase="mineru",
                status="item_start",
                done=done_count,
                total=len(target_parts),
                item=f"OCR + ritagli ({part_names})",
            )
            print(
                f"Avvio MinerU ({tier}, ocr-mode={ocr_mode}, ONNX CPU) su {len(staged_inputs)} PDF: {part_names}...",
                flush=True,
            )

            cmd = [
                str(mineru_bin),
                "parse",
                *[str(link_pdf) for _, _, _, link_pdf in staged_inputs],
                "-o",
                str(out_dir),
                "--tier",
                tier,
                "--ocr-mode",
                ocr_mode,
                "-f",
                "zip",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                print(
                    f"ERRORE MinerU (exit={res.returncode}):\n{(res.stderr or res.stdout)[-1000:]}",
                    file=sys.stderr,
                    flush=True,
                )
                return 2

            dt_total = round(time.monotonic() - t0, 2)
            per_item_dt = round(dt_total / max(1, len(staged_inputs)), 2)

            for part, pdf_path, part_cache, link_pdf in staged_inputs:
                expected_zip = out_dir / f"{link_pdf.stem}.zip"
                if not expected_zip.exists():
                    # Cerca eventuale zip annidato
                    matches = list(out_dir.rglob(f"{link_pdf.stem}.zip"))
                    expected_zip = matches[0] if matches else expected_zip
                if not expected_zip.exists():
                    print(
                        f"ERRORE: output zip MinerU non trovato per Parte {part['part_id']} ({expected_zip})",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 2
                idx = extract_zip_to_cache(expected_zip, part_cache, pdf_path, ocr_mode=ocr_mode)
                done_count += 1
                print(
                    f"  ✓ MinerU Parte {part['part_id']}: OCR completato, "
                    f"{len(idx['images'])} immagini ritagliate -> {part_cache}",
                    flush=True,
                )
                emit_progress(
                    phase="mineru",
                    status="item_done",
                    done=done_count,
                    total=len(target_parts),
                    item=f"Parte {part['part_id']}",
                    duration=per_item_dt,
                )

    emit_progress(phase="mineru", status="done", done=len(target_parts), total=len(target_parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
