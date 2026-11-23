#!/usr/bin/env python3
"""Safely inserts/updates #include "cap<N>.typ" in main.typ with git commit or timestamped backup."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.paths import TEMPLATES_DIR, load_config


def run_git(git_bin: str, repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [git_bin, "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
    )


def ensure_local_copy(typst_dir: Path, shared_dir_str: str) -> None:
    if typst_dir.exists():
        return
    if shared_dir_str:
        shared_dir = Path(shared_dir_str).expanduser()
        if shared_dir.exists():
            print(f"Creazione copia locale in {typst_dir} da {shared_dir}...")
            shutil.copytree(
                shared_dir,
                typst_dir,
                ignore=shutil.ignore_patterns("_recupero", ".localtypst*", ".preview-*"),
            )
            return
    typst_dir.mkdir(parents=True, exist_ok=True)


def ensure_helpers(typst_dir: Path, templates_dir: Path, figs_dir_name: str) -> None:
    helpers_dst = typst_dir / "fig-helpers.typ"
    if not helpers_dst.exists():
        shutil.copy2(templates_dir / "fig-helpers.typ", helpers_dst)
    figs_dir = typst_dir / figs_dir_name
    figs_dir.mkdir(parents=True, exist_ok=True)
    idx_dst = figs_dir / "_index.typ"
    if not idx_dst.exists():
        shutil.copy2(templates_dir / "figs_index.typ", idx_dst)


def ensure_main_file(main_path: Path, materia_name: str) -> None:
    if main_path.exists():
        return
    main_path.parent.mkdir(parents=True, exist_ok=True)
    main_path.write_text(
        f"""#import "@preview/cetz:0.3.4": canvas, draw
#set page(paper: "a4", margin: 1.5cm, numbering: "1", number-align: bottom + right)
#set text(lang: "it", size: 11pt)
#set par(justify: true)
#set heading(numbering: "1.")

#let nota(body) = block(width: 100%, fill: luma(240), inset: 8pt, radius: 3pt, body)
#let circ = sym.degree
#let varphi = $phi$

#align(center)[
  #text(size: 20pt, weight: "bold")[Appunti di {materia_name}]
]
#outline(indent: auto, depth: 2)
#pagebreak()
""",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Inserimento sicuro del capitolo in main.typ")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()

    _, cfg = load_config(args.config)
    general = cfg["general"]
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))

    typst_dir = Path(manifest["typst_project_dir"])
    ensure_local_copy(typst_dir, manifest.get("shared_source_dir", ""))
    main_path = typst_dir / manifest["main_file"]
    cap_num = manifest["capitolo"]
    slug = manifest.get("chapter_slug") or f"cap{cap_num}"
    cap_rel = f"{slug}.typ"
    include_line = f'#include "{cap_rel}"'

    templates_dir = TEMPLATES_DIR
    ensure_main_file(main_path, manifest["materia_name"])
    ensure_helpers(typst_dir, templates_dir, manifest["figs_dir"])

    is_git = bool(manifest.get("is_git_repo"))
    git_bin = general["git_bin"]
    undo_cmd = ""

    if is_git:
        run_git(git_bin, typst_dir, "add", "-A")
        pre = run_git(git_bin, typst_dir, "commit", "-m", f"chore: pre-insert {manifest['materia']} cap{cap_num}")
        pre_hash = run_git(git_bin, typst_dir, "rev-parse", "--short", "HEAD").stdout.strip()
        undo_cmd = f"git -C '{typst_dir}' reset --hard {pre_hash}"
    else:
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup_dir = typst_dir / "_backup_appunti"
        backup_dir.mkdir(parents=True, exist_ok=True)
        bak_main = backup_dir / f"{main_path.name}.{ts}.bak"
        shutil.copy2(main_path, bak_main)
        undo_cmd = f"cp '{bak_main}' '{main_path}'"

    content = main_path.read_text(encoding="utf-8")
    if include_line not in content:
        if content and not content.endswith("\n"):
            content += "\n"
        content += include_line + "\n"
        main_path.write_text(content, encoding="utf-8")

    # Verifica compilazione dell'intero main.typ
    pdf_out = Path(general["work_dir"]) / f"{manifest['materia']}_compile_check.pdf"
    res = subprocess.run(
        [general["typst_bin"], "compile", str(main_path), str(pdf_out)],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print(f"ERRORE compilazione {main_path}:\n{res.stderr}", file=sys.stderr)
        print(f"Per annullare: {undo_cmd}", file=sys.stderr)
        return 2

    if is_git:
        run_git(git_bin, typst_dir, "add", "-A")
        run_git(git_bin, typst_dir, "commit", "-m", f"feat: add {manifest['materia']} cap{cap_num}")

    print(f"Inserito {include_line} in {main_path} e compilato con successo.")
    print(f"Comando per annullare: {undo_cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
