#!/usr/bin/env python3
"""Figure generator driven by manifest.json, config.toml, and #img-flag calls.

Scans chapter part .typ files for #img-flag("<id>", pagina: <n>, desc: "..."),
renders the PDF page with pdftoppm, generates CeTZ code via agy + prompts/figure.md,
validates on a minimal Typst wrapper (max 2 retries), and falls back to PNG on failure.
Never modifies the source .typ prose files; only writes figs/<id>.typ|png and figs/_index.typ.
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
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.agy_client import call_llm_oneshot, create_llm_session
from core.paths import TEMPLATES_DIR, load_config


def emit_progress(**payload: Any) -> None:
    print(f"@@PROGRESS {json.dumps(payload, ensure_ascii=False)}", flush=True)


FLAG_RX = re.compile(
    r'#img-flag\(\s*"([^"]+)"(?P<args>[^)]*)\)',
    re.DOTALL,
)
PAGE_RX = re.compile(r'pagina\s*:\s*(\d+)')
SRC_RX = re.compile(r'src\s*:\s*"([^"]+)"')
KIND_RX = re.compile(r'kind\s*:\s*"([^"]+)"')
DESC_RX = re.compile(r'desc\s*:\s*"((?:[^"\\]|\\.)*)"', re.DOTALL)
PART_FROM_ID_RX = re.compile(r'^[^-]+-c\d+-p([^-]+)-\d+$', re.IGNORECASE)

HYBRID_KEEP_CROP_RX = re.compile(
    r"\b(fotografia|foto|microscopia|fotogramma|vista\s+reale|sezione\s+costruttiva|disegno\s+costruttivo|"
    r"esploso|catalogo|reale|pezzo\s+rotto|frattografia)\b",
    re.IGNORECASE,
)


def should_use_direct_crop(fl: dict[str, Any], mode: str, has_crop: bool) -> bool:
    if not has_crop:
        return False
    if mode == "mineru":
        return True
    if mode == "cetz":
        return False
    kind = (fl.get("kind") or "").strip().lower()
    if kind in ("photo", "complex", "crop", "image", "real"):
        return True
    if kind in ("diagram", "schema", "graph", "chart", "cetz"):
        return False
    return bool(HYBRID_KEEP_CROP_RX.search(fl.get("desc") or ""))


def parse_flags(typ_files: list[Path]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tf in typ_files:
        if not tf.exists():
            continue
        text = tf.read_text(encoding="utf-8")
        # Fallback part_id from filename (e.g. p1.typ -> "1", cap12_p3.typ -> "3")
        fn_m = re.search(r'p(\d+[a-z]?)\.typ$', tf.name, re.IGNORECASE)
        file_part = fn_m.group(1).lower() if fn_m else None

        for m in FLAG_RX.finditer(text):
            fid = m.group(1).strip()
            if fid in seen:
                continue
            seen.add(fid)
            args_str = m.group("args")
            pm = PAGE_RX.search(args_str)
            sm = SRC_RX.search(args_str)
            km = KIND_RX.search(args_str)
            dm = DESC_RX.search(args_str)
            id_m = PART_FROM_ID_RX.match(fid)
            part_id = id_m.group(1).lower() if id_m else file_part
            flags.append(
                {
                    "id": fid,
                    "part_id": part_id,
                    "pagina": int(pm.group(1)) if pm else 1,
                    "src": sm.group(1).strip() if sm else None,
                    "kind": km.group(1).strip().lower() if km else None,
                    "desc": dm.group(1).replace('\\"', '"') if dm else "",
                    "source_file": str(tf),
                }
            )
    return flags


def resolve_mineru_crop(mineru_dir: Path | None, part_id: str | None, src: str | None) -> Path | None:
    if not mineru_dir or not part_id or not src:
        return None
    fname = Path(src).name
    cand = mineru_dir / f"p{part_id}" / "images" / fname
    if cand.exists() and cand.is_file():
        return cand
    # Cerca anche case-insensitive o in sottocartelle
    pdir = mineru_dir / f"p{part_id}" / "images"
    if pdir.exists():
        for f in pdir.iterdir():
            if f.name.lower() == fname.lower():
                return f
    return None


def render_pdf_page(pdftoppm_bin: str, pdf_path: Path, page: int, out_png: Path) -> None:
    prefix = out_png.with_suffix("")
    res = subprocess.run(
        [
            pdftoppm_bin,
            "-png",
            "-r",
            "180",
            "-f",
            str(page),
            "-l",
            str(page),
            "-singlefile",
            str(pdf_path),
            str(prefix),
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0 or not out_png.exists():
        raise RuntimeError(f"pdftoppm fallito su {pdf_path.name} pag. {page}: {res.stderr.strip()}")


def extract_cetz_code(raw: str) -> str:
    m = re.search(r"```(?:typst)?\s*\n(.*?)```", raw, re.DOTALL)
    code = m.group(1).strip() if m else raw.strip()
    # Ensure #import cetz is present at top for standalone #include
    if '@preview/cetz:' not in code:
        code = '#import "@preview/cetz:0.3.4": canvas, draw\n' + code
    return code + "\n"


def compile_wrapper(typst_bin: str, code: str, tmp_dir: Path) -> tuple[bool, str]:
    sub = tmp_dir / "figs"
    sub.mkdir(exist_ok=True)
    macros_src = TEMPLATES_DIR / "cetz-macros.typ"
    if macros_src.exists():
        if not (sub / "_macros.typ").exists():
            shutil.copy2(macros_src, sub / "_macros.typ")
        if not (tmp_dir / "cetz-macros.typ").exists():
            shutil.copy2(macros_src, tmp_dir / "cetz-macros.typ")
    wrapper = sub / "wrapper.typ"
    pdf_out = sub / "wrapper.pdf"
    body = code
    if "@preview/cetz:" not in body and "_macros.typ" not in body and "cetz-macros.typ" not in body:
        body = '#import "@preview/cetz:0.3.4": canvas, draw\n' + body
    wrapper.write_text(
        "#set page(width: auto, height: auto, margin: 0.4cm)\n" + body,
        encoding="utf-8",
    )
    res = subprocess.run(
        [typst_bin, "compile", "--root", str(tmp_dir), str(wrapper), str(pdf_out)],
        capture_output=True,
        text=True,
    )
    return res.returncode == 0, res.stderr.strip()


def build_batches(flags: list[dict[str, Any]], max_batch: int) -> list[list[dict[str, Any]]]:
    """Raggruppa per parte e suddivide parti grandi in blocchi equilibrati <= max_batch."""
    max_batch = max(1, max_batch)
    part_groups: list[list[dict[str, Any]]] = []
    for fl in flags:
        if not part_groups or part_groups[-1][0].get("part_id") != fl.get("part_id"):
            part_groups.append([fl])
        else:
            part_groups[-1].append(fl)

    batches: list[list[dict[str, Any]]] = []
    for grp in part_groups:
        n = len(grp)
        if n <= max_batch:
            batches.append(grp)
        else:
            n_chunks = (n + max_batch - 1) // max_batch
            base, rem = divmod(n, n_chunks)
            idx = 0
            for c in range(n_chunks):
                sz = base + (1 if c < rem else 0)
                batches.append(grp[idx : idx + sz])
                idx += sz
    return batches


def build_figure_prompt(
    prompt_text: str,
    img_path: Path,
    desc: str,
    include_full_rules: bool,
    is_cropped: bool = False,
) -> str:
    if is_cropped:
        header = (
            f"Leggi l'immagine già ritagliata con read_file: {img_path}\n"
            f"Rigenera in CeTZ questa figura (descrizione: \"{desc}\").\n"
            "Usa dimensioni intrinseche in cm (es. #canvas(length: 1cm, ...)), MAI width: 100%.\n"
            "Rispondi esclusivamente con il codice Typst/CeTZ (nessun commento o spiegazione)."
        )
    else:
        header = (
            f"Leggi l'immagine della slide con read_file: {img_path}\n"
            f"Estrai e rigenera in CeTZ SOLO la figura descritta come: \"{desc}\".\n"
            "Usa dimensioni intrinseche in cm (es. #canvas(length: 1cm, ...)), MAI width: 100%.\n"
            "Rispondi esclusivamente con il codice Typst/CeTZ (nessun commento o spiegazione)."
        )
    if include_full_rules:
        return f"{header}\n\n{prompt_text}"
    return (
        f"{header}\n"
        "Applica esattamente tutte le regole CeTZ e il formato di risposta già specificati nel primo messaggio "
        "(niente #import/#let/for/calc/path/variabili dentro il canvas, archi con anchor: \"origin\" e start < stop, "
        "etichette con text(size: ...pt)[...])."
    )


def build_fix_prompt(prev_code: str, error_msg: str, in_same_session: bool) -> str:
    if in_same_session:
        return (
            f"Il codice CeTZ appena prodotto ha dato questo errore di compilazione Typst:\n"
            f"{error_msg[:600]}\n\n"
            "Correggi l'errore mantenendo la figura fedele all'originale e rispettando tutte le regole CeTZ. "
            "Rispondi SOLO con il blocco Typst/CeTZ corretto."
        )
    return (
        f"Il codice CeTZ precedente ha dato questo errore di compilazione Typst:\n"
        f"{error_msg[:600]}\n\n"
        f"Codice precedente:\n```typst\n{prev_code}\n```\n\n"
        "Correggi l'errore mantenendo la figura fedele all'originale. "
        "Rispondi SOLO con il codice Typst/CeTZ corretto."
    )


def update_figs_index(figs_dir: Path) -> None:
    entries: dict[str, tuple[str, str]] = {}
    for p in sorted(figs_dir.iterdir()):
        if not p.is_file() or p.name.startswith("_"):
            continue
        if p.suffix == ".typ":
            entries[p.stem] = ("typ", p.name)
        elif p.suffix in (".png", ".jpg", ".jpeg") and p.stem not in entries:
            entries[p.stem] = ("png", p.name)

    lines = [
        "// Registro automatico delle figure generate (non modificare a mano)",
    ]
    if not entries:
        lines.append("#let fig-registry = (:)")
    else:
        lines.append("#let fig-registry = (")
        for fid, (ftype, fname) in sorted(entries.items()):
            lines.append(f'  "{fid}": (type: "{ftype}", path: "{fname}"),')
        lines.append(")")
    idx_path = figs_dir / "_index.typ"
    tmp_idx = figs_dir / "_index.typ.tmp"
    tmp_idx.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp_idx.replace(idx_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera figure CeTZ da #img-flag")
    parser.add_argument("manifest", type=Path, help="Percorso a manifest.json")
    parser.add_argument("typ_files", nargs="+", type=Path, help="File .typ delle parti da scansionare")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--force", action="store_true", help="Rigenera anche figure esistenti")
    parser.add_argument("--mineru-dir", type=Path, default=None, help="Directory cache MinerU del capitolo")
    parser.add_argument(
        "--figures-mode",
        choices=("cetz", "hybrid", "mineru"),
        default=None,
        help="Modalità figure: cetz (tutto CeTZ), hybrid (ritaglio foto/complesse + CeTZ schemi), mineru (solo ritagli)",
    )
    parser.add_argument(
        "--backend",
        choices=("agy", "gemini-api"),
        default=None,
        help="Backend LLM per generazione figure CeTZ",
    )
    args = parser.parse_args()

    _, cfg = load_config(args.config)
    general = cfg["general"]
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    figures_mode = args.figures_mode or general.get("figures_mode", "hybrid")
    backend = args.backend or general.get("llm_backend", "agy")

    prompt_file = Path(general["prompts_dir"]) / "figure.md"
    if not prompt_file.exists():
        print(f"Errore: prompt figure mancante: {prompt_file}", file=sys.stderr)
        return 1
    prompt_text = prompt_file.read_text(encoding="utf-8").strip()

    typst_dir = Path(manifest["typst_project_dir"])
    figs_dir = typst_dir / manifest["figs_dir"]
    figs_dir.mkdir(parents=True, exist_ok=True)
    macros_src = TEMPLATES_DIR / "cetz-macros.typ"
    if macros_src.exists() and not (figs_dir / "_macros.typ").exists():
        shutil.copy2(macros_src, figs_dir / "_macros.typ")

    part_pdfs = {
        p["part_id"].lower(): Path(p["pdf"])
        for p in manifest["parts"]
        if p.get("pdf")
    }

    flags = parse_flags(args.typ_files)
    max_retries = int(general.get("figure_max_retries", 2))
    batch_size = int(general.get("figure_batch_size", 5))

    report: dict[str, Any] = {
        "materia": manifest["materia"],
        "capitolo": manifest["capitolo"],
        "figures_mode": figures_mode,
        "backend": backend,
        "total_flags": len(flags),
        "succeeded": [],
        "direct_crops": [],
        "skipped_existing": [],
        "fallback_png": [],
        "errors": [],
    }

    direct_crop_flags: list[tuple[dict[str, Any], Path]] = []
    pending_cetz_flags: list[dict[str, Any]] = []
    skipped_flags: list[dict[str, Any]] = []
    for fl in flags:
        fid = fl["id"]
        out_typ = figs_dir / f"{fid}.typ"
        out_img_exists = any((figs_dir / f"{fid}{ext}").exists() for ext in (".jpg", ".jpeg", ".png"))
        if not args.force and (out_typ.exists() or (figures_mode == "mineru" and out_img_exists)):
            skipped_flags.append(fl)
            continue
        crop_src = resolve_mineru_crop(args.mineru_dir, fl["part_id"], fl.get("src"))
        if should_use_direct_crop(fl, figures_mode, crop_src is not None):
            assert crop_src is not None
            direct_crop_flags.append((fl, crop_src))
        else:
            pending_cetz_flags.append(fl)

    to_generate = len(pending_cetz_flags)
    emit_progress(
        phase="figures",
        status="plan",
        done=0,
        total=len(flags),
        to_generate=to_generate,
        done_generated=0,
    )

    done_count = 0
    done_generated = 0

    for fl in skipped_flags:
        fid = fl["id"]
        report["skipped_existing"].append(fid)
        done_count += 1
        emit_progress(
            phase="figures",
            status="item_skip",
            done=done_count,
            total=len(flags),
            to_generate=to_generate,
            done_generated=done_generated,
            item=fid,
        )

    # Inserimento istantaneo (0s) dei ritagli MinerU diretti (modalita mineru o hybrid su foto/sezioni complesse)
    for fl, crop_src in direct_crop_flags:
        fid = fl["id"]
        dst_img = figs_dir / f"{fid}{crop_src.suffix.lower()}"
        shutil.copy2(crop_src, dst_img)
        old_typ = figs_dir / f"{fid}.typ"
        if old_typ.exists():
            old_typ.unlink()
        report["direct_crops"].append(fid)
        update_figs_index(figs_dir)
        done_count += 1
        print(
            f"  ✓ Figura {fid} inserita direttamente da ritaglio MinerU ({crop_src.name}, modalità {figures_mode})",
            flush=True,
        )
        emit_progress(
            phase="figures",
            status="item_skip",
            done=done_count,
            total=len(flags),
            to_generate=to_generate,
            done_generated=done_generated,
            item=f"{fid} (ritaglio MinerU)",
        )

    batches = build_batches(pending_cetz_flags, batch_size)
    if batches:
        print(
            f"Sessioni {backend} pianificate: {len(batches)} blocchi per {to_generate} figure CeTZ "
            f"(modalità={figures_mode}, max {batch_size}/sessione).",
            flush=True,
        )

    for b_idx, batch in enumerate(batches, start=1):
        with tempfile.TemporaryDirectory(prefix="appunti_fig_") as tmp:
            tmp_dir = Path(tmp)
            print(
                f"\n--- Blocco figure {b_idx}/{len(batches)} "
                f"({len(batch)} figure: {[x['id'] for x in batch]}) ---",
                flush=True,
            )
            with create_llm_session(
                general,
                model=general["model_figures"],
                add_dirs=tmp_dir,
                backend=backend,
            ) as sess:
                for fl in batch:
                    fid = fl["id"]
                    out_typ = figs_dir / f"{fid}.typ"
                    out_png = figs_dir / f"{fid}.png"

                    pid = fl["part_id"]
                    crop_src = resolve_mineru_crop(args.mineru_dir, pid, fl.get("src"))
                    pdf_path = part_pdfs.get(pid) if pid else None
                    if not crop_src and (not pdf_path or not pdf_path.exists()):
                        msg = f"Sorgente non trovata per flag {fid} (parte={pid})"
                        report["errors"].append({"id": fid, "error": msg})
                        done_count += 1
                        done_generated += 1
                        emit_progress(
                            phase="figures",
                            status="item_done",
                            done=done_count,
                            total=len(flags),
                            to_generate=to_generate,
                            done_generated=done_generated,
                            item=fid,
                            ok=False,
                        )
                        continue

                    t0 = time.monotonic()
                    emit_progress(
                        phase="figures",
                        status="item_start",
                        done=done_count,
                        total=len(flags),
                        to_generate=to_generate,
                        done_generated=done_generated,
                        item=fid,
                        desc=fl["desc"][:80],
                    )
                    if crop_src:
                        print(f"Generazione figura {fid} (ritaglio MinerU: {crop_src.name})...", flush=True)
                        page_img = tmp_dir / f"{fid}_crop{crop_src.suffix.lower()}"
                        shutil.copy2(crop_src, page_img)
                        is_cropped = True
                    else:
                        assert pdf_path is not None
                        print(f"Generazione figura {fid} (pag. {fl['pagina']} di {pdf_path.name})...", flush=True)
                        page_img = tmp_dir / f"{fid}_page.png"
                        is_cropped = False
                        try:
                            render_pdf_page(general["pdftoppm_bin"], pdf_path, fl["pagina"], page_img)
                        except Exception as exc:
                            report["errors"].append({"id": fid, "error": str(exc)})
                            done_count += 1
                            done_generated += 1
                            emit_progress(
                                phase="figures",
                                status="item_done",
                                done=done_count,
                                total=len(flags),
                                to_generate=to_generate,
                                done_generated=done_generated,
                                item=fid,
                                ok=False,
                                duration=round(time.monotonic() - t0, 2),
                            )
                            continue

                    first_in_session = sess.turns == 0
                    fig_prompt = build_figure_prompt(
                        prompt_text,
                        page_img,
                        fl["desc"],
                        include_full_rules=first_in_session,
                        is_cropped=is_cropped,
                    )
                    rc, out, err = sess.send(fig_prompt, files=[page_img])
                    used_stream = rc == 0
                    if rc != 0:
                        print(f"  Avviso sessione ({err[:100]}), riprovo in modalità singola...", flush=True)
                        full_prompt = build_figure_prompt(
                            prompt_text,
                            page_img,
                            fl["desc"],
                            include_full_rules=True,
                            is_cropped=is_cropped,
                        )
                        rc, out, err = call_llm_oneshot(
                            general,
                            model=general["model_figures"],
                            add_dirs=tmp_dir,
                            prompt=full_prompt,
                            files=[page_img],
                            backend=backend,
                        )
                        used_stream = False

                    code = extract_cetz_code(out) if rc == 0 and out.strip() else ""
                    ok, comp_err = (
                        compile_wrapper(general["typst_bin"], code, tmp_dir)
                        if code
                        else (False, f"LLM error: {err[:300]}")
                    )

                    for attempt in range(1, max_retries + 1):
                        if ok:
                            break
                        print(f"  Retry {attempt}/{max_retries} per {fid}...", flush=True)
                        emit_progress(
                            phase="figures",
                            status="item_retry",
                            done=done_count,
                            total=len(flags),
                            to_generate=to_generate,
                            done_generated=done_generated,
                            item=fid,
                            attempt=attempt,
                            max_retries=max_retries,
                        )
                        fix_prompt = build_fix_prompt(code, comp_err, in_same_session=used_stream)
                        if used_stream:
                            rc, out, err = sess.send(fix_prompt)
                            if rc != 0:
                                used_stream = False
                                rc, out, err = call_llm_oneshot(
                                    general,
                                    model=general["model_figures"],
                                    add_dirs=tmp_dir,
                                    prompt=build_fix_prompt(code, comp_err, in_same_session=False),
                                    files=[page_img],
                                    backend=backend,
                                )
                        else:
                            rc, out, err = call_llm_oneshot(
                                general,
                                model=general["model_figures"],
                                add_dirs=tmp_dir,
                                prompt=fix_prompt,
                                files=[page_img],
                                backend=backend,
                            )
                        if rc == 0 and out.strip():
                            code = extract_cetz_code(out)
                            ok, comp_err = compile_wrapper(general["typst_bin"], code, tmp_dir)
                        else:
                            comp_err = f"LLM retry error: {err[:300]}"

                    if ok:
                        out_typ.write_text(code, encoding="utf-8")
                        for old_fb in (out_png, figs_dir / f"{fid}.jpg"):
                            if old_fb.exists():
                                old_fb.unlink()
                        report["succeeded"].append(fid)
                        update_figs_index(figs_dir)
                        print(f"  ✓ Figura {fid} generata in {time.monotonic() - t0:.1f}s (visibile live in LocalTypst)", flush=True)
                    else:
                        fb_ext = page_img.suffix.lower()
                        fb_dst = figs_dir / f"{fid}{fb_ext}"
                        shutil.copy2(page_img, fb_dst)
                        report["fallback_png"].append({"id": fid, "reason": comp_err[:200]})
                        update_figs_index(figs_dir)
                        print(f"  Fallback immagine ({fb_dst.name}) per {fid}: {comp_err[:120]}", flush=True)

                    done_count += 1
                    done_generated += 1
                    emit_progress(
                        phase="figures",
                        status="item_done",
                        done=done_count,
                        total=len(flags),
                        to_generate=to_generate,
                        done_generated=done_generated,
                        item=fid,
                        ok=ok,
                        duration=round(time.monotonic() - t0, 2),
                    )

    update_figs_index(figs_dir)
    emit_progress(
        phase="figures",
        status="done",
        done=len(flags),
        total=len(flags),
        to_generate=to_generate,
        done_generated=done_generated,
    )

    work_dir = Path(general["work_dir"]).expanduser()
    work_dir.mkdir(parents=True, exist_ok=True)
    rep_path = work_dir / f"{manifest['materia']}_cap{manifest['capitolo']}_figures_report.json"
    rep_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"Figure: {len(report['succeeded'])} create, "
        f"{len(report['skipped_existing'])} già presenti, "
        f"{len(report['fallback_png'])} fallback PNG, "
        f"{len(report['errors'])} errori.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
