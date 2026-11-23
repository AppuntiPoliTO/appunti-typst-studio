"""Client condiviso per agy CLI: sessioni stream-json riutilizzabili + fallback oneshot."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class AgyStreamSession:
    """Mantiene una singola sessione agy stream-json viva per un blocco di richieste.

    Evita avvii a freddo ripetuti del Language Server (e picchi RAM/zram) mantenendo
    al tempo stesso il contesto fresco riavviando per blocchi da 4-5 figure.
    """

    def __init__(self, agy_bin: str, model: str, add_dirs: list[Path] | Path) -> None:
        self.agy_bin = agy_bin
        self.model = model
        if isinstance(add_dirs, Path):
            self.add_dirs = [add_dirs]
        else:
            self.add_dirs = list(add_dirs)
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

        cmd = [
            self.agy_bin,
            "--model",
            self.model,
            "--disable-slash-commands",
        ]
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

    def send(self, prompt: str) -> tuple[int, str, str]:
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
