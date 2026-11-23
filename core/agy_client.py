"""Client LLM dual-backend: `agy` CLI (stream-json riutilizzabile) oppure Gemini REST API diretta."""

from __future__ import annotations

import base64
import json
import mimetypes
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .models import resolve_api_model
from .paths import load_gemini_api_key

TEXT_SUFFIXES = {".txt", ".md", ".typ", ".json", ".toml", ".tex", ".csv"}


def _guess_mime(path: Path) -> str:
    suf = path.suffix.lower()
    if suf == ".png":
        return "image/png"
    if suf in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suf == ".webp":
        return "image/webp"
    if suf == ".pdf":
        return "application/pdf"
    if suf == ".mp3":
        return "audio/mp3"
    if suf == ".wav":
        return "audio/wav"
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "application/octet-stream"


class AgyStreamSession:
    """Mantiene una singola sessione agy stream-json viva per un blocco di richieste."""

    def __init__(self, agy_bin: str, model: str, add_dirs: list[Path] | Path) -> None:
        self.agy_bin = agy_bin
        self.model = model
        self.add_dirs = [add_dirs] if isinstance(add_dirs, Path) else list(add_dirs)
        self.proc: subprocess.Popen[str] | None = None
        self.turns = 0

    def start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        seen: list[str] = []
        for d in self.add_dirs:
            ds = str(d)
            if ds and ds not in seen:
                seen.append(ds)

        cmd = [self.agy_bin, "--model", self.model, "--disable-slash-commands"]
        for ds in seen:
            cmd.extend(["--add-dir", ds])
        cmd.extend(
            [
                "--dangerously-skip-permissions",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "-p",
                "",
            ]
        )
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.turns = 0

    def send(self, prompt: str, files: list[Path] | None = None) -> tuple[int, str, str]:
        del files  # agy legge direttamente i file sul disco tramite read_file
        self.start()
        assert self.proc and self.proc.stdin and self.proc.stdout
        try:
            payload = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False)
            self.proc.stdin.write(payload + "\n")
            self.proc.stdin.flush()
        except Exception as exc:
            self.close()
            return 1, "", f"stream write error: {exc}"

        for raw in self.proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("event") == "result":
                res = ev.get("result") or {}
                self.turns += 1
                if res.get("status") == "SUCCESS":
                    return 0, str(res.get("response") or ""), ""
                err_msg = str(res.get("error") or f"status={res.get('status')}")
                return 1, str(res.get("response") or ""), err_msg

        err_out = ""
        if self.proc.stderr:
            try:
                err_out = self.proc.stderr.read()[:400]
            except Exception:
                pass
        self.close()
        return 1, "", err_out or "stream-json EOF imprevisto"

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        except Exception:
            pass
        self.proc = None

    def __enter__(self) -> "AgyStreamSession":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


class GeminiApiSession:
    """Sessione multi-turno diretta verso Gemini REST API (0 processo locale, ~15 MB RAM)."""

    def __init__(self, api_key: str, model: str, timeout: int = 180) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.history: list[dict[str, Any]] = []
        self.turns = 0

    def send(self, prompt: str, files: list[Path] | None = None) -> tuple[int, str, str]:
        parts: list[dict[str, Any]] = []
        for fpath in files or []:
            if not fpath or not fpath.exists():
                continue
            if fpath.suffix.lower() in TEXT_SUFFIXES:
                content = fpath.read_text(encoding="utf-8", errors="replace")
                parts.append({"text": f"--- FILE: {fpath.name} ---\n{content}\n--- END FILE ---"})
            else:
                b64 = base64.b64encode(fpath.read_bytes()).decode("ascii")
                parts.append({"inline_data": {"mime_type": _guess_mime(fpath), "data": b64}})
        parts.append({"text": prompt})

        user_msg = {"role": "user", "parts": parts}
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        body_bytes = json.dumps({"contents": [*self.history, user_msg]}).encode("utf-8")

        last_err = ""
        for attempt in range(1, 4):
            req = urllib.request.Request(
                url,
                data=body_bytes,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                cands = data.get("candidates") or []
                if not cands:
                    last_err = f"Nessun candidato restituito: {str(data)[:250]}"
                else:
                    resp_parts = cands[0].get("content", {}).get("parts") or []
                    text = "".join(p.get("text", "") for p in resp_parts).strip()
                    if text:
                        self.history.append(user_msg)
                        self.history.append({"role": "model", "parts": [{"text": text}]})
                        self.turns += 1
                        return 0, text, ""
                    last_err = "Risposta Gemini API vuota"
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", errors="replace")[:350]
                last_err = f"HTTP {exc.code}: {err_body}"
                if exc.code not in (429, 500, 502, 503, 504):
                    break
            except Exception as exc:
                last_err = str(exc)
            if attempt < 3:
                time.sleep(2 * attempt)

        return 1, "", last_err

    def close(self) -> None:
        self.history.clear()

    def __enter__(self) -> "GeminiApiSession":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def create_llm_session(
    general: dict[str, Any],
    *,
    model: str,
    add_dirs: list[Path] | Path,
    backend: str | None = None,
) -> AgyStreamSession | GeminiApiSession:
    eff_backend = (backend or general.get("llm_backend") or "agy").strip().lower()
    if eff_backend == "gemini-api":
        api_key = load_gemini_api_key(general)
        if not api_key:
            raise RuntimeError(
                "Backend 'gemini-api' selezionato ma nessuna API key trovata "
                "(imposta GEMINI_API_KEY o ~/.config/appunti/gemini.key)."
            )
        api_model = resolve_api_model(model, fallback=general.get("gemini_api_model", "gemini-2.5-pro"))
        return GeminiApiSession(api_key=api_key, model=api_model)
    return AgyStreamSession(general["agy_bin"], model, add_dirs)


def call_agy_oneshot(
    agy_bin: str,
    model: str,
    add_dirs: list[Path] | Path,
    prompt: str,
) -> tuple[int, str, str]:
    dirs = [add_dirs] if isinstance(add_dirs, Path) else list(add_dirs)
    seen: list[str] = []
    for d in dirs:
        ds = str(d)
        if ds and ds not in seen:
            seen.append(ds)
    cmd = [agy_bin, "--model", model, "--disable-slash-commands"]
    for ds in seen:
        cmd.extend(["--add-dir", ds])
    cmd.extend(["--dangerously-skip-permissions", "-p", prompt])
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode, res.stdout, res.stderr


def call_llm_oneshot(
    general: dict[str, Any],
    *,
    model: str,
    add_dirs: list[Path] | Path,
    prompt: str,
    files: list[Path] | None = None,
    backend: str | None = None,
) -> tuple[int, str, str]:
    eff_backend = (backend or general.get("llm_backend") or "agy").strip().lower()
    if eff_backend == "gemini-api":
        with create_llm_session(general, model=model, add_dirs=add_dirs, backend="gemini-api") as sess:
            return sess.send(prompt, files=files)
    return call_agy_oneshot(general["agy_bin"], model, add_dirs, prompt)
