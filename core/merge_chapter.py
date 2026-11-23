#!/usr/bin/env python3
"""Unisce parti (_parts/<slug>/p*.typ) o un file capitolo separato dentro un file .typ di destinazione.

Caratteristiche di sicurezza:
- Preserva SEMPRE eventuale prosa già presente nel file di destinazione (mai sovrascritture distruttive)
- Aggiunge automaticamente `#import "fig-helpers.typ": fig-safe, img-flag` se necessario
- Opzione `--strip-h1` per evitare un titolo `= ...` duplicato quando si accoda a un capitolo già avviato
- Crea sempre backup in `_backup_appunti/` prima di toccare i file
- Verifica la compilazione di `main.typ` con `typst compile` e ripristina tutto in caso di errore
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.paths import load_config

INCLUDE_PART_RX = re.compile(r'^\s*#include\s+"(_parts/[^"]+)"\s*$')
IMPORT_HELPER_RX = re.compile(
    r'^\s*#import\s+"(?:\.\./)*(?:figs/_helpers|fig-helpers)\.typ"\s*:.*$\n?',
    re.MULTILINE,
)
H1_LINE_RX = re.compile(r"^\s*=\s+.*$\n?", re.MULTILINE)


def _strip_chunk(text: str, strip_h1: bool) -> str:
    body = IMPORT_HELPER_RX.sub("", text).strip()
    if strip_h1:
        lines = body.splitlines()
        if lines and re.match(r"^\s*=\s+[^=]", lines[0]):
            body = "\n".join(lines[1:]).lstrip()
    return body


def _ensure_fig_helper_import(text: str) -> str:
    if ("#img-flag" in text or "img-flag(" in text or "#fig-safe" in text or "fig-safe(" in text):
        if "figs/_helpers.typ" not in text and "fig-helpers.typ" not in text:
            return '#import "figs/_helpers.typ": fig-safe, img-flag\n\n' + text
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Unisce parti o file generati dentro un capitolo .typ")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--target",
        default="",
        help="Nome file .typ di destinazione nel progetto (es. 'collegamenti saldati.typ')",
    )
    parser.add_argument(
        "--source",
        default="",
        help="File .typ sorgente separato da accodare in --target (es. 'saldature.typ') oppure vuoto per usare _parts/<slug>/",
    )
    parser.add_argument(
        "--strip-h1",
        action="store_true",
        help="Rimuove eventuale titolo '=' di primo livello dalle parti accodate se la destinazione ha già un '='",
    )
    parser.add_argument("--keep-parts", action="store_true", help="Non eliminare _parts/<slug> o il file sorgente dopo l'unione")
    args = parser.parse_args()

    _, cfg = load_config(args.config)
    general = cfg["general"]
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))

    typst_dir = Path(manifest["typst_project_dir"])
    slug = manifest["chapter_slug"]
    target_name = args.target.strip() or f"{slug}.typ"
    target_file = typst_dir / target_name
    main_path = typst_dir / manifest["main_file"]

    ts = time.strftime("%Y%m%d_%H%M%S")
    bak_dir = typst_dir / "_backup_appunti"
    bak_dir.mkdir(parents=True, exist_ok=True)

    backups: list[tuple[Path, Path]] = []

    def backup_file(p: Path, tag: str) -> None:
        if p.exists():
            dst = bak_dir / f"{p.name}.{ts}.{tag}.bak"
            shutil.copy2(p, dst)
            backups.append((p, dst))

    def rollback_all() -> None:
        for orig, bak in backups:
            if bak.exists():
                shutil.copy2(bak, orig)

    # --- Modalità A: Accoda un file .typ separato (es. saldature.typ) dentro target_file (es. collegamenti saldati.typ) ---
    source_name = args.source.strip()
    if source_name and source_name != target_name:
        source_file = typst_dir / source_name
        if not source_file.exists():
            print(f"Errore: file sorgente non trovato: {source_file}", file=sys.stderr, flush=True)
            return 1

        # Se il sorgente contiene a sua volta solo #include "_parts/...", espandi quelle parti
        src_raw = source_file.read_text(encoding="utf-8")
        src_lines = src_raw.splitlines()
        src_part_paths: list[Path] = []
        expanded_blocks: list[str] = []
        for ln in src_lines:
            m = INCLUDE_PART_RX.match(ln)
            if m:
                pf = typst_dir / m.group(1)
                if pf.exists():
                    src_part_paths.append(pf)
                    expanded_blocks.append(pf.read_text(encoding="utf-8"))
            else:
                expanded_blocks.append(ln)
        src_combined = "\n".join(expanded_blocks)

        existing_target = target_file.read_text(encoding="utf-8") if target_file.exists() else ""
        target_has_h1 = bool(re.search(r"^\s*=\s+[^=]", existing_target, re.MULTILINE))
        appended_body = _strip_chunk(src_combined, strip_h1=(args.strip_h1 or target_has_h1))
        if not appended_body:
            print(f"Il file sorgente {source_name} è vuoto.", flush=True)
            return 0

        backup_file(target_file, "premerge")
        backup_file(source_file, "merged_src")
        backup_file(main_path, "premerge")

        if existing_target.strip():
            new_target = existing_target.rstrip() + "\n\n#v(1em)\n\n" + appended_body + "\n"
        else:
            new_target = appended_body + "\n"
        new_target = _ensure_fig_helper_import(new_target)
        target_file.write_text(new_target, encoding="utf-8")

        # Aggiorna main.typ: assicurati che includa target_name e rimuovi il vecchio #include source_name se !keep_parts
        if main_path.exists():
            mtxt = main_path.read_text(encoding="utf-8")
            inc_src = f'#include "{source_name}"'
            inc_dst = f'#include "{target_name}"'
            mlines = mtxt.splitlines()
            if not args.keep_parts:
                mlines = [ln for ln in mlines if ln.strip() != inc_src]
            if not any(ln.strip() == inc_dst for ln in mlines):
                mlines.append(inc_dst)
            main_path.write_text("\n".join(mlines) + "\n", encoding="utf-8")

        pdf_out = Path(general["work_dir"]).expanduser() / f"{manifest['materia']}_merge_check.pdf"
        res = subprocess.run(
            [general["typst_bin"], "compile", str(main_path), str(pdf_out)],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            rollback_all()
            print(f"ERRORE compilazione dopo unione (tutto ripristinato):\n{res.stderr}", file=sys.stderr, flush=True)
            return 2

        if not args.keep_parts:
            if source_file.exists():
                source_file.unlink()
            for pf in src_part_paths:
                if pf.exists():
                    pf.unlink()
            # Rimuovi cartella _parts/<source_slug> se vuota
            src_parts_dir = typst_dir / "_parts" / Path(source_name).stem
            if src_parts_dir.exists() and not any(src_parts_dir.iterdir()):
                src_parts_dir.rmdir()

        print(
            f"✓ Accodato '{source_name}' dentro '{target_name}' e compilato con successo (backup in {bak_dir}).",
            flush=True,
        )
        return 0

    # --- Modalità B: Unione in-place delle parti _parts/<slug>/p*.typ dentro target_file (preservando la prosa esistente!) ---
    parts_dir = typst_dir / "_parts" / slug
    part_files_disk = sorted(parts_dir.glob("p*.typ")) if parts_dir.exists() else []
    if not target_file.exists() and not part_files_disk:
        print(f"Errore: né {target_file.name} né parti in _parts/{slug}/ trovati.", file=sys.stderr, flush=True)
        return 1

    existing_txt = target_file.read_text(encoding="utf-8") if target_file.exists() else ""
    lines = existing_txt.splitlines()
    target_has_prose_h1 = any(
        re.match(r"^\s*=\s+[^=]", ln) for ln in lines if not INCLUDE_PART_RX.match(ln)
    )

    merged_parts_count = 0
    included_paths: set[Path] = set()
    out_blocks: list[str] = []

    for ln in lines:
        m = INCLUDE_PART_RX.match(ln)
        if m:
            pf = typst_dir / m.group(1)
            if pf.exists():
                included_paths.add(pf.resolve())
                body = _strip_chunk(
                    pf.read_text(encoding="utf-8"),
                    strip_h1=(args.strip_h1 or target_has_prose_h1),
                )
                if body:
                    out_blocks.append(body)
                    merged_parts_count += 1
            continue
        out_blocks.append(ln)

    # Eventuali parti in _parts/<slug>/ non ancora incluse esplicitamente vengono accodate in fondo
    for pf in part_files_disk:
        if pf.resolve() not in included_paths:
            body = _strip_chunk(
                pf.read_text(encoding="utf-8"),
                strip_h1=(args.strip_h1 or target_has_prose_h1),
            )
            if body:
                if out_blocks and out_blocks[-1].strip():
                    out_blocks.append("")
                out_blocks.append(body)
                merged_parts_count += 1

    if merged_parts_count == 0:
        print(f"{target_file.name} non contiene parti in _parts/{slug}/ da unire.", flush=True)
        return 0

    backup_file(target_file, "premerge")
    new_content = "\n".join(out_blocks).strip() + "\n"
    new_content = _ensure_fig_helper_import(new_content)
    target_file.write_text(new_content, encoding="utf-8")

    pdf_out = Path(general["work_dir"]).expanduser() / f"{manifest['materia']}_merge_check.pdf"
    res = subprocess.run(
        [general["typst_bin"], "compile", str(main_path), str(pdf_out)],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        rollback_all()
        print(f"ERRORE compilazione dopo merge (ripristinato):\n{res.stderr}", file=sys.stderr, flush=True)
        return 2

    if not args.keep_parts and parts_dir.exists():
        shutil.rmtree(parts_dir)

    print(
        f"✓ Unite {merged_parts_count} parti dentro '{target_file.name}' e compilato con successo (backup in {bak_dir}).",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
