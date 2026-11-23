#!/usr/bin/env python3
"""Audio/video transcription script driven by manifest.json and config.toml.

Extracts mono 16kHz audio to a temporary directory, splits into ~10 min chunks
with overlap, transcribes verbatim via Google AI Studio (gemini-3.5-transcribe
when GEMINI_API_KEY or gemini_api_key_file is present) or agy as fallback,
and caches .txt next to the source file.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.paths import load_config


def emit_progress(**payload: Any) -> None:
    print(f"@@PROGRESS {json.dumps(payload, ensure_ascii=False)}", flush=True)


def load_gemini_api_key(general: dict[str, Any]) -> str | None:
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key
    key_file_cfg = general.get("gemini_api_key_file", "")
    if key_file_cfg:
        kf = Path(key_file_cfg).expanduser()
        if kf.exists():
            val = kf.read_text(encoding="utf-8").strip()
            if val:
                return val
    return None


def probe_duration(ffprobe_bin: str, media_path: Path) -> float:
    res = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(media_path),
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError(f"Audio/video illeggibile: {media_path} ({res.stderr.strip()})")
    return float(res.stdout.strip())


def extract_chunk(
    ffmpeg_bin: str,
    media_path: Path,
    out_audio: Path,
    start_sec: float,
    duration_sec: float,
) -> None:
    cmd = [
        ffmpeg_bin,
        "-y",
        "-ss",
        f"{start_sec:.3f}",
        "-t",
        f"{duration_sec:.3f}",
        "-i",
        str(media_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "64k",
        str(out_audio),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not out_audio.exists() or out_audio.stat().st_size == 0:
        raise RuntimeError(
            f"Estrazione audio fallita per {media_path} @ {start_sec}s: {res.stderr[-300:]}"
        )


def transcribe_chunk_gemini_api(
    api_key: str,
    model: str,
    prompt_text: str,
    audio_path: Path,
    chunk_idx: int,
    total_chunks: int,
    max_retries: int = 3,
) -> str:
    extra = (
        f"\nSegmento {chunk_idx}/{total_chunks} dell'audio. "
        "Trascrivi tutto ciò che senti in questo file."
        if total_chunks > 1
        else ""
    )
    b64_audio = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": f"{prompt_text}{extra}"},
                    {"inline_data": {"mime_type": "audio/mp3", "data": b64_audio}},
                ]
            }
        ]
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    data = json.dumps(payload).encode("utf-8")

    last_err = ""
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            parts = body["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return text
            last_err = "risposta Gemini vuota"
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:300]}"
        except Exception as exc:
            last_err = str(exc)

        if attempt < max_retries:
            time.sleep(2 * attempt)

    raise RuntimeError(
        f"Trascrizione Gemini API fallita dopo {max_retries} tentativi per {audio_path.name}: {last_err}"
    )


def transcribe_chunk_agy(
    agy_bin: str,
    model: str,
    prompt_text: str,
    audio_path: Path,
    chunk_idx: int,
    total_chunks: int,
    max_retries: int = 3,
) -> str:
    extra = (
        f"\nQuesto è il segmento {chunk_idx}/{total_chunks} dell'audio. "
        "Se c'è una leggera sovrapposizione con il segmento precedente, trascrivi comunque tutto ciò che senti in questo file."
        if total_chunks > 1
        else ""
    )
    full_prompt = (
        f"Usa read_file per leggere e ascoltare il file audio {audio_path}.{extra}\n\n"
        f"{prompt_text}"
    )

    last_err = ""
    for attempt in range(1, max_retries + 1):
        res = subprocess.run(
            [
                agy_bin,
                "--model",
                model,
                "--add-dir",
                str(audio_path.parent),
                "--dangerously-skip-permissions",
                "-p",
                full_prompt,
            ],
            capture_output=True,
            text=True,
        )
        out = res.stdout.strip()
        if res.returncode == 0 and out:
            return out
        last_err = res.stderr.strip() or f"exit={res.returncode}, output vuoto"
        if attempt < max_retries:
            time.sleep(2 * attempt)

    raise RuntimeError(
        f"Trascrizione agy fallita dopo {max_retries} tentativi per {audio_path.name}: {last_err[:300]}"
    )


def transcribe_part(
    part_id: str,
    media_path: Path,
    general: dict[str, Any],
    prompt_text: str,
    api_key: str | None,
    chunk_offset: int = 0,
    total_chunks_all: int = 0,
) -> tuple[Path, int]:
    txt_path = media_path.with_suffix(".txt")
    if txt_path.exists() and txt_path.stat().st_size > 0:
        return txt_path, 0

    ffmpeg_bin = general["ffmpeg_bin"]
    ffprobe_bin = str(Path(ffmpeg_bin).with_name("ffprobe"))
    chunk_sec = int(general.get("audio_chunk_seconds", 600))
    overlap_sec = int(general.get("audio_overlap_seconds", 8))

    total_dur = probe_duration(ffprobe_bin, media_path)
    step = max(60, chunk_sec - overlap_sec)
    n_chunks = 1 if total_dur <= chunk_sec else math.ceil((total_dur - overlap_sec) / step)
    eff_total = total_chunks_all if total_chunks_all > 0 else n_chunks

    transcripts: list[str] = []
    done_here = 0
    with tempfile.TemporaryDirectory(prefix="appunti_audio_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        for idx in range(n_chunks):
            start = idx * step
            dur = min(chunk_sec, total_dur - start)
            if dur <= 0.5:
                break
            t0 = time.monotonic()
            item_label = f"Parte {part_id} · chunk {idx + 1}/{n_chunks}"
            emit_progress(
                phase="transcribe",
                status="item_start",
                done=chunk_offset + done_here,
                total=eff_total,
                item=item_label,
            )
            chunk_file = tmp_path / f"chunk_{idx + 1:03d}.mp3"
            extract_chunk(ffmpeg_bin, media_path, chunk_file, start, dur)
            backend = (
                f"Gemini API ({general.get('gemini_transcribe_model', 'gemini-3.5-transcribe')})"
                if api_key
                else f"agy ({general['model_transcribe']})"
            )
            print(
                f"  -> Trascrizione chunk {idx + 1}/{n_chunks} "
                f"({start:.0f}s - {start + dur:.0f}s) con {backend}...",
                flush=True,
            )
            if api_key:
                text = transcribe_chunk_gemini_api(
                    api_key,
                    general.get("gemini_transcribe_model", "gemini-3.5-transcribe"),
                    prompt_text,
                    chunk_file,
                    idx + 1,
                    n_chunks,
                )
            else:
                text = transcribe_chunk_agy(
                    general["agy_bin"],
                    general["model_transcribe"],
                    prompt_text,
                    chunk_file,
                    idx + 1,
                    n_chunks,
                )
            transcripts.append(text.strip())
            done_here += 1
            emit_progress(
                phase="transcribe",
                status="item_done",
                done=chunk_offset + done_here,
                total=eff_total,
                item=item_label,
                duration=round(time.monotonic() - t0, 2),
            )

    if not transcripts:
        raise RuntimeError(f"Nessun testo trascritto per {media_path}")

    txt_path.write_text("\n\n".join(transcripts) + "\n", encoding="utf-8")
    return txt_path, done_here


def main() -> int:
    parser = argparse.ArgumentParser(description="Trascrizione audio/video da manifest.json")
    parser.add_argument("manifest", type=Path, help="Percorso al file manifest.json")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--parts", default="", help="Lista parti separate da virgola (es. 1,2)")
    args = parser.parse_args()

    _, cfg = load_config(args.config)
    general = cfg["general"]
    api_key = load_gemini_api_key(general)

    prompt_file = Path(general["prompts_dir"]) / "trascrizione.md"
    if not prompt_file.exists():
        print(f"Errore: prompt mancante: {prompt_file}", file=sys.stderr)
        return 1
    prompt_text = prompt_file.read_text(encoding="utf-8").strip()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    allowed = {x.strip().lower() for x in args.parts.split(",") if x.strip()} if args.parts else None
    target_parts = [
        p
        for p in manifest["parts"]
        if p.get("needs_transcription")
        and p.get("media")
        and (allowed is None or p["part_id"].lower() in allowed)
    ]

    # Calcola in anticipo il numero totale di chunk per barra progressi / ETA
    ffprobe_bin = str(Path(general["ffmpeg_bin"]).with_name("ffprobe"))
    chunk_sec = int(general.get("audio_chunk_seconds", 600))
    overlap_sec = int(general.get("audio_overlap_seconds", 8))
    step = max(60, chunk_sec - overlap_sec)
    total_chunks_all = 0
    for part in target_parts:
        try:
            dur = probe_duration(ffprobe_bin, Path(part["media"]))
            total_chunks_all += 1 if dur <= chunk_sec else math.ceil((dur - overlap_sec) / step)
        except Exception:
            total_chunks_all += 1

    emit_progress(phase="transcribe", status="plan", done=0, total=total_chunks_all)

    created: list[str] = []
    changed = False
    chunk_offset = 0

    for part in target_parts:
        media_path = Path(part["media"])
        print(f"Trascrizione parte {part['part_id']}: {media_path.name}", flush=True)
        try:
            txt_path, n_done = transcribe_part(
                part["part_id"],
                media_path,
                general,
                prompt_text,
                api_key,
                chunk_offset=chunk_offset,
                total_chunks_all=total_chunks_all,
            )
            chunk_offset += n_done
        except Exception as exc:
            print(f"ERRORE trascrizione parte {part['part_id']}: {exc}", file=sys.stderr, flush=True)
            return 2

        part["txt"] = str(txt_path)
        part["needs_transcription"] = False
        part["status"] = "ready" if part.get("pdf") else "missing_pdf"
        created.append(str(txt_path))
        changed = True

    if changed:
        manifest["missing"]["parts_needing_transcription"] = [
            p["part_id"] for p in manifest["parts"] if p["needs_transcription"]
        ]
        manifest["missing"]["parts_without_txt"] = [
            p["part_id"] for p in manifest["parts"] if p["pdf"] and not p["txt"]
        ]
        args.manifest.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    emit_progress(phase="transcribe", status="done", done=total_chunks_all, total=total_chunks_all)
    if created:
        print(f"Trascrizioni create ({len(created)}): {created}", flush=True)
    else:
        print("Nessuna trascrizione necessaria (tutte le parti hanno già il .txt o non hanno media).", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
