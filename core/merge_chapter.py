#!/usr/bin/env python3
"""Merges _parts/<slug>/p*.typ into a single <slug>.typ file after review."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.paths import load_config

INCLUDE_RX = re.compile(r'^\s*#include\s+"([^"]+)"\s*$')
IMPORT_HELPER_RX = re.compile(r'^\s*#import\s+"(?:\.\./)*fig-helpers\.typ"\s*:.*$\n?', re.MULTILINE)


def main() -> int:
    parser = argparse.ArgumentParser(description="Unisce le parti di un capitolo in un unico .typ")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--keep-parts", action="store_true", help="Non eliminare _parts/<slug> dopo il merge")
    args = parser.parse_args()

    _, cfg = load_config(args.config)
    general = cfg["general"]
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))

    typst_dir = Path(manifest["typst_project_dir"])
    slug = manifest["chapter_slug"]
    cap_file = typst_dir / f"{slug}.typ"
    if not cap_file.exists():
        print(f"Errore: file capitolo non trovato: {cap_file}", file=sys.stderr)
        return 1

    lines = cap_file.read_text(encoding="utf-8").splitlines()
    part_paths: list[Path] = []
    for ln in lines:
        m = INCLUDE_RX.match(ln)
        if m:
            p = typst_dir / m.group(1)
            if p.exists():
                part_paths.append(p)

    if not part_paths:
        print(f"{cap_file.name} non contiene #include di parti da unire.")
        return 0

    # Backup prima dell'unione
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak_dir = typst_dir / "_backup_appunti"
    bak_dir.mkdir(parents=True, exist_ok=True)
    bak_cap = bak_dir / f"{cap_file.name}.{ts}.premerge.bak"
    shutil.copy2(cap_file, bak_cap)

    merged_chunks = ['#import "fig-helpers.typ": fig-safe, img-flag', ""]
    for p in part_paths:
        body = IMPORT_HELPER_RX.sub("", p.read_text(encoding="utf-8")).strip()
        if body:
            merged_chunks.append(body)
            merged_chunks.append("")

    cap_file.write_text("\n".join(merged_chunks).rstrip() + "\n", encoding="utf-8")

    # Verifica compilazione
    main_path = typst_dir / manifest["main_file"]
    pdf_out = Path(general["work_dir"]).expanduser() / f"{manifest['materia']}_merge_check.pdf"
    res = subprocess.run(
        [general["typst_bin"], "compile", str(main_path), str(pdf_out)],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        shutil.copy2(bak_cap, cap_file)
        print(f"ERRORE compilazione dopo merge (ripristinato):\n{res.stderr}", file=sys.stderr)
        return 2

    if not args.keep_parts:
        parts_dir = typst_dir / "_parts" / slug
        if parts_dir.exists():
            shutil.rmtree(parts_dir)

    print(f"Unite {len(part_paths)} parti in {cap_file} (backup: {bak_cap}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
