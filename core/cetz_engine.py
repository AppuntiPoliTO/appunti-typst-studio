"""Motore unificato CeTZ Studio: conversione immagini/screenshot/.typ in codice CeTZ verificato.

Risolve i vecchi problemi di CeTZ Studio:
- Usa AgyStreamSession a blocchi (evitando 5 processi agy paralleli da 2+ GB di RAM)
- Aggiunge sempre fino a 2 retry di compilazione Typst nella stessa sessione viva
- Supporta cancellazione pulita e callback di avanzamento reale per GUI / ETA
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .agy_client import AgyStreamSession, GeminiApiSession, call_llm_oneshot, create_llm_session
from .paths import TEMPLATES_DIR

IMAGE_CALL_RX = re.compile(r'(#?)image\(\s*"([^"]+)"[^)]*\)')


def extract_cetz_code(raw: str, include_import: bool = False) -> str:
    m = re.search(r"```(?:typst)?\s*\n(.*?)```", raw, re.DOTALL)
    code = m.group(1).strip() if m else raw.strip()
    code = re.sub(r"\\?var(epsilon|phi)\b", r"\1", code)
    if include_import and "@preview/cetz:" not in code and "cetz-macros.typ" not in code:
        code = '#import "@preview/cetz:0.3.4": canvas, draw\n' + code
    return code


def render_cetz_png(
    typst_bin: str,
    code: str,
    out_dir: Path,
    stem: str,
    ppi: int = 180,
) -> tuple[bool, Path, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    figs_sub = out_dir / "figs"
    figs_sub.mkdir(exist_ok=True)
    macros_src = TEMPLATES_DIR / "cetz-macros.typ"
    if macros_src.exists():
        if not (figs_sub / "_macros.typ").exists():
            shutil.copy2(macros_src, figs_sub / "_macros.typ")
        if not (out_dir / "cetz-macros.typ").exists():
            shutil.copy2(macros_src, out_dir / "cetz-macros.typ")
    tp = figs_sub / f"{stem}.typ"
    pp = out_dir / f"{stem}.png"
    header = '#set page(width: auto, height: auto, margin: 0.4cm)\n'
    if "@preview/cetz:" not in code and "_macros.typ" not in code and "cetz-macros.typ" not in code:
        header += '#import "@preview/cetz:0.3.4": canvas, draw\n'
    tp.write_text(header + code + "\n", encoding="utf-8")
    res = subprocess.run(
        [typst_bin, "compile", "--root", str(out_dir), "--format", "png", "--ppi", str(ppi), str(tp), str(pp)],
        capture_output=True,
        text=True,
    )
    return res.returncode == 0, pp, res.stderr.strip()


def build_cetz_prompt(
    prompt_text: str,
    img_path: Path,
    *,
    include_full_rules: bool = True,
    skip_non_study: bool = False,
    extra_instruction: str = "",
) -> str:
    skip_clause = " Se NON è uno schema tecnico/scientifico o di studio rispondi SOLO SKIP. Altrimenti:" if skip_non_study else ""
    extra = f"\nIstruzioni aggiuntive: {extra_instruction}" if extra_instruction else ""
    lead = f"Leggi {img_path} con read_file e rigenerala in CeTZ.{skip_clause}{extra}"
    if include_full_rules:
        return f"{lead}\n\n{prompt_text}"
    return (
        f"{lead}\n"
        "Applica esattamente tutte le regole CeTZ e il formato di risposta già specificati nel primo messaggio "
        "(niente #import/#let/for/calc/path/variabili dentro il canvas, archi con anchor: \"origin\" e start < stop, "
        "etichette con text(size: ...pt)[...]). Rispondi SOLO con il blocco ```typst."
    )


def build_fix_prompt(prev_code: str, error_msg: str, in_same_session: bool) -> str:
    if in_same_session:
        return (
            f"Il codice CeTZ appena generato ha prodotto questo errore di compilazione Typst:\n"
            f"{error_msg[:600]}\n\n"
            "Correggi l'errore mantenendo fedele il diagramma e rispettando tutte le regole CeTZ. "
            "Rispondi SOLO con il blocco ```typst corretto."
        )
    return (
        f"Il codice CeTZ seguente ha prodotto un errore di compilazione Typst:\n"
        f"```typst\n{prev_code}\n```\n\n"
        f"Errore Typst:\n{error_msg[:600]}\n\n"
        "Correggi l'errore mantenendo fedele il diagramma. Rispondi SOLO con il blocco ```typst corretto."
    )


def convert_single_image(
    *,
    general: dict[str, Any] | None = None,
    agy_bin: str,
    typst_bin: str,
    model: str,
    prompt_text: str,
    img_path: Path,
    out_dir: Path,
    stem: str,
    session: AgyStreamSession | GeminiApiSession | None = None,
    backend: str = "agy",
    skip_non_study: bool = False,
    extra_instruction: str = "",
    max_retries: int = 2,
    status_cb: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    t0 = time.monotonic()
    img_dir = img_path.parent
    gen = dict(general or {})
    gen.setdefault("agy_bin", agy_bin)
    use_stream = session is not None
    first_in_sess = (session.turns == 0) if session is not None else True

    pr = build_cetz_prompt(
        prompt_text,
        img_path,
        include_full_rules=first_in_sess,
        skip_non_study=skip_non_study,
        extra_instruction=extra_instruction,
    )
    if session is not None:
        rc, out, err = session.send(pr, files=[img_path])
        if rc != 0:
            use_stream = False
            full_pr = build_cetz_prompt(
                prompt_text,
                img_path,
                include_full_rules=True,
                skip_non_study=skip_non_study,
                extra_instruction=extra_instruction,
            )
            rc, out, err = call_llm_oneshot(gen, model=model, add_dirs=img_dir, prompt=full_pr, files=[img_path], backend=backend)
    else:
        rc, out, err = call_llm_oneshot(gen, model=model, add_dirs=img_dir, prompt=pr, files=[img_path], backend=backend)

    if rc != 0:
        return {
            "ok": False,
            "status": f"Errore {backend}",
            "code": "",
            "png": "",
            "err": err.strip(),
            "duration": round(time.monotonic() - t0, 2),
        }

    out_s = out.strip()
    if skip_non_study and (out_s == "SKIP" or (out_s.startswith("SKIP") and "#canvas" not in out_s)):
        return {
            "ok": False,
            "status": "Ignorata",
            "code": "",
            "png": "",
            "err": "",
            "duration": round(time.monotonic() - t0, 2),
        }

    code = extract_cetz_code(out_s)
    ok, png_path, comp_err = render_cetz_png(typst_bin, code, out_dir, stem)

    for attempt in range(1, max_retries + 1):
        if ok:
            break
        if status_cb:
            status_cb(f"Fix Typst ({attempt}/{max_retries})…")
        fix_pr = build_fix_prompt(code, comp_err, in_same_session=use_stream)
        if use_stream and session is not None:
            rc, out, err = session.send(fix_pr)
            if rc != 0:
                use_stream = False
                rc, out, err = call_llm_oneshot(
                    gen,
                    model=model,
                    add_dirs=img_dir,
                    prompt=build_fix_prompt(code, comp_err, in_same_session=False),
                    files=[img_path],
                    backend=backend,
                )
        else:
            rc, out, err = call_llm_oneshot(
                gen, model=model, add_dirs=img_dir, prompt=fix_pr, files=[img_path], backend=backend
            )
        if rc != 0 or not out.strip():
            break
        code = extract_cetz_code(out)
        ok, png_path, comp_err = render_cetz_png(typst_bin, code, out_dir, stem)

    return {
        "ok": ok,
        "status": "Pronto" if ok else "Errore Typst",
        "code": code,
        "png": str(png_path) if ok else "",
        "err": "" if ok else comp_err,
        "duration": round(time.monotonic() - t0, 2),
    }


def refine_cetz_code(
    *,
    general: dict[str, Any] | None = None,
    agy_bin: str,
    typst_bin: str,
    model: str,
    current_code: str,
    instruction: str,
    out_dir: Path,
    stem: str,
    img_path: Path | None = None,
    backend: str = "agy",
    max_retries: int = 2,
) -> dict[str, Any]:
    t0 = time.monotonic()
    gen = dict(general or {})
    gen.setdefault("agy_bin", agy_bin)
    add_dirs: list[Path] = []
    files: list[Path] = []
    ref_line = ""
    if img_path and img_path.exists():
        add_dirs.append(img_path.parent)
        files.append(img_path)
        ref_line = f"Immagine originale di riferimento (se utile, usa read_file): {img_path}\n"

    pr = (
        f"{ref_line}"
        f"Codice CeTZ attuale:\n```typst\n{current_code}\n```\n\n"
        f"Modifica richiesta: {instruction}\n"
        "Rispondi SOLO con il blocco ```typst aggiornato (senza #import/#set/#let/for/calc dentro il canvas)."
    )
    with create_llm_session(gen, model=model, add_dirs=add_dirs, backend=backend) as sess:
        rc, out, err = sess.send(pr, files=files)
        if rc != 0:
            return {
                "ok": False,
                "status": f"Errore {backend}",
                "code": current_code,
                "png": "",
                "err": err.strip(),
                "duration": round(time.monotonic() - t0, 2),
            }
        code = extract_cetz_code(out)
        ok, png_path, comp_err = render_cetz_png(typst_bin, code, out_dir, stem)
        for _ in range(max_retries):
            if ok:
                break
            rc, out, _ = sess.send(build_fix_prompt(code, comp_err, in_same_session=True))
            if rc != 0 or not out.strip():
                break
            code = extract_cetz_code(out)
            ok, png_path, comp_err = render_cetz_png(typst_bin, code, out_dir, stem)

    return {
        "ok": ok,
        "status": "Pronto" if ok else "Errore Typst",
        "code": code,
        "png": str(png_path) if ok else "",
        "err": "" if ok else comp_err,
        "duration": round(time.monotonic() - t0, 2),
    }


def parse_typ_images(typ_path: Path, screenshots_dir: Path) -> tuple[str, list[str], list[tuple[int, int, int, str, Path]]]:
    txt = typ_path.read_text(encoding="utf-8")
    lines = txt.splitlines(keepends=True)
    base = typ_path.parent
    tasks: list[tuple[int, int, int, str, Path]] = []
    for li, ln in enumerate(lines):
        prev = lines[li - 1] if li > 0 else ""
        if "no-cetz" in ln or "no-cetz" in prev:
            continue
        for m in IMAGE_CALL_RX.finditer(ln):
            rel = m.group(2)
            p = base / rel
            if not p.exists():
                p = screenshots_dir / Path(rel).name
            if p.exists():
                tasks.append((li, m.start(), m.end(), m.group(1), p))
    return txt, lines, tasks


def convert_typ_batches(
    tasks: list[tuple[int, int, int, str, Path]],
    *,
    general: dict[str, Any] | None = None,
    agy_bin: str,
    typst_bin: str,
    model: str,
    prompt_text: str,
    out_dir: Path,
    backend: str = "agy",
    batch_size: int = 5,
    skip_non_study: bool = True,
    max_retries: int = 2,
    progress_fn: Callable[[int, int, str, float | None], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Converte le immagini trovate in un .typ usando sessioni riutilizzabili a blocchi (agy o gemini-api)."""
    results: list[dict[str, Any]] = []
    total = len(tasks)
    batch_size = max(1, batch_size)
    gen = dict(general or {})
    gen.setdefault("agy_bin", agy_bin)

    for start in range(0, total, batch_size):
        if cancel_event and cancel_event.is_set():
            break
        batch = tasks[start : start + batch_size]
        dirs = list({t[4].parent for t in batch})
        with create_llm_session(gen, model=model, add_dirs=dirs, backend=backend) as sess:
            for t in batch:
                if cancel_event and cancel_event.is_set():
                    break
                li, s, _e, _h, img_path = t
                stem = f"typ_{li}_{s}"
                if progress_fn:
                    progress_fn(len(results), total, img_path.name, None)
                res = convert_single_image(
                    general=gen,
                    agy_bin=agy_bin,
                    typst_bin=typst_bin,
                    model=model,
                    prompt_text=prompt_text,
                    img_path=img_path,
                    out_dir=out_dir,
                    stem=stem,
                    session=sess,
                    backend=backend,
                    skip_non_study=skip_non_study,
                    max_retries=max_retries,
                )
                res["task"] = t
                res["stem"] = stem
                results.append(res)
                if progress_fn:
                    progress_fn(len(results), total, img_path.name, res.get("duration"))
    return results


def apply_typ_replacements(
    typ_path: Path,
    txt: str,
    lines: list[str],
    selections: list[tuple[tuple[int, int, int, str, Path], str, bool]],
) -> tuple[int, int]:
    out_lines = list(lines)
    replaced = 0
    by_line: dict[int, list[tuple[int, int, str]]] = {}
    for (li, s, e, h, _p), code, keep_orig in selections:
        if not keep_orig and code:
            c = code
            if not h and c.startswith("#"):
                c = c[1:]
            elif h and not c.startswith("#"):
                c = "#" + c
            by_line.setdefault(li, []).append((s, e, c))
            replaced += 1

    for li, edits in by_line.items():
        ln = out_lines[li]
        for s, e, c in sorted(edits, reverse=True):
            ln = ln[:s] + c + ln[e:]
        out_lines[li] = ln

    out = "".join(out_lines)
    if replaced and "@preview/cetz" not in out:
        out = '#import "@preview/cetz:0.3.4": canvas, draw\n' + out
    if replaced:
        bak = typ_path.with_suffix(typ_path.suffix + ".bak")
        bak.write_text(txt, encoding="utf-8")
        typ_path.write_text(out, encoding="utf-8")
    return replaced, len(selections) - replaced
