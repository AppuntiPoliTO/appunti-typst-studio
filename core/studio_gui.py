#!/usr/bin/env python3
"""Interfaccia grafica Libadwaita / GTK 4 per Appunti Typst + CeTZ.

Funzionalità principali:
- Design nativo GNOME / Libadwaita (light & dark mode)
- Selezione automatica dei capitoli disponibili per materia
- Selezione granulare delle parti da generare (checkbox + preset Tutte / Pronte / Nessuna)
- Barra dei progressi reale (per fase, parte, chunk audio e figura CeTZ)
- Stima del tempo rimanente (ETA) dinamica + cronometro live
- Stepper visivo delle fasi, notifiche desktop, toast in-app e log formattato
- Interruzione pulita dell'intero albero di processi (inclusi figures.py e agy)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.cetz_studio_view import CeTZStudioView
from core.paths import ASSETS_DIR, BIN_DIR, append_subject_to_config, load_config

CORE_DIR = REPO_ROOT / "core"
APPUNTI_BIN = BIN_DIR / "appunti"

PROGRESS_PREFIX = "@@PROGRESS "
FLAG_COUNT_RX = re.compile(r'#img-flag\(\s*"([^"]+)"')
FLAG_SRC_LOOKUP_RX = re.compile(r'#img-flag\(\s*"([^"]+)"([^)]*)\)', re.DOTALL)
SRC_PARAM_RX = re.compile(r'src\s*:\s*"([^"]+)"')
FIG_MODES = [("hybrid", "Ibrida (CeTZ schemi + ritagli foto)"), ("cetz", "Tutto CeTZ"), ("mineru", "Solo ritagli MinerU (veloce)")]
LLM_BACKENDS = [("agy", "Antigravity CLI (agy)"), ("gemini-api", "Gemini REST API (API key)")]

CUSTOM_CSS = """
.hero-card {
  padding: 18px 20px;
  border-radius: 14px;
}

.meta-chip {
  font-size: 11px;
  font-weight: 600;
  padding: 3px 9px;
  border-radius: 999px;
  background-color: color-mix(in srgb, currentColor 8%, transparent);
  color: color-mix(in srgb, currentColor 78%, transparent);
}

.stage-pill {
  font-size: 11px;
  font-weight: 600;
  padding: 4px 10px;
  border-radius: 999px;
  background-color: color-mix(in srgb, currentColor 7%, transparent);
  color: color-mix(in srgb, currentColor 58%, transparent);
  transition: all 180ms ease;
}

.stage-pill.stage-active {
  background-color: var(--accent-bg-color);
  color: var(--accent-fg-color);
}

.stage-pill.stage-done {
  background-color: color-mix(in srgb, var(--success-bg-color) 18%, transparent);
  color: var(--success-color);
}

.stage-pill.stage-skip {
  opacity: 0.42;
}

.status-badge {
  font-size: 11px;
  font-weight: 700;
  padding: 4px 10px;
  border-radius: 999px;
}

.badge-ready {
  background-color: color-mix(in srgb, var(--success-bg-color) 18%, transparent);
  color: var(--success-color);
}

.badge-generated {
  background-color: color-mix(in srgb, var(--accent-bg-color) 18%, transparent);
  color: var(--accent-color);
}

.badge-warn {
  background-color: color-mix(in srgb, var(--warning-bg-color) 20%, transparent);
  color: var(--warning-color);
}

.badge-err {
  background-color: color-mix(in srgb, var(--error-bg-color) 18%, transparent);
  color: var(--error-color);
}

progressbar.appunti-progress trough {
  min-height: 10px;
  border-radius: 999px;
}

progressbar.appunti-progress progress {
  min-height: 10px;
  border-radius: 999px;
}

.stat-pill {
  font-size: 12px;
  font-weight: 500;
  padding: 4px 10px;
  border-radius: 8px;
  background-color: color-mix(in srgb, currentColor 6%, transparent);
  font-variant-numeric: tabular-nums;
}

.percent-label {
  font-size: 15px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.log-card {
  border-radius: 14px;
}

.log-header {
  padding: 8px 14px;
  border-bottom: 1px solid color-mix(in srgb, currentColor 10%, transparent);
}

textview.log-view {
  font-family: monospace;
  font-size: 11.5px;
  padding: 10px 14px;
}

.part-row-box {
  padding: 10px 14px;
}

.cetz-side-card {
  padding: 12px 14px;
  border-radius: 12px;
}

.cetz-preview-card {
  padding: 12px 14px;
  border-radius: 12px;
}
"""


def send_notify(title: str, body: str, urgency: str = "normal") -> None:
    try:
        subprocess.Popen(
            ["notify-send", "-a", "Appunti Typst", "-u", urgency, title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    sec = int(round(seconds))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    if m > 0:
        return f"{m:02d}:{s:02d}"
    return f"00:{s:02d}"


def fmt_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "Calcolo stima…"
    sec = int(round(seconds))
    if sec < 5:
        return "Quasi pronto…"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"~{h}h {m:02d}m rimanenti"
    if m >= 2:
        return f"~{m}m {s:02d}s rimanenti"
    if m == 1:
        return f"~1m {s:02d}s rimanenti"
    return f"~{s}s rimanenti"


def discover_available_chapters(cfg_mat: dict[str, Any]) -> list[tuple[int, str]]:
    """Trova tutti i capitoli/moduli presenti nella cartella sorgente della materia."""
    source_dir = Path(cfg_mat["source_dir"]).expanduser()
    if not source_dir.exists():
        return []

    layout = cfg_mat.get("chapter_layout", "subfolders")
    chapters: dict[int, str] = {}

    if layout == "subfolders":
        rx = re.compile(cfg_mat["chapter_dir_pattern"])
        for entry in sorted(source_dir.iterdir()):
            if not entry.is_dir():
                continue
            m = rx.match(entry.name)
            if m:
                num = int(m.group(1))
                title = m.group(2).strip() if m.lastindex and m.lastindex >= 2 else entry.name
                chapters[num] = title
    else:
        pdf_rx = re.compile(cfg_mat["pdf_pattern"])
        for f in sorted(source_dir.iterdir()):
            if not f.is_file():
                continue
            m = pdf_rx.match(f.name)
            if m:
                num = int(m.group(1))
                chapters.setdefault(num, f"Modulo {num}")

    return sorted(chapters.items(), key=lambda x: x[0])


@dataclass
class PhasePlan:
    name: str
    status: str = "pending"  # pending | active | done | skip
    done: int = 0
    total: int = 0
    prior_sec_per_item: float = 10.0
    durations: list[float] = field(default_factory=list)
    item_started_at: float | None = None
    current_item: str = ""
    known_exact_total: bool = False

    def avg_sec(self) -> float:
        if not self.durations:
            return self.prior_sec_per_item
        if len(self.durations) == 1:
            return 0.35 * self.prior_sec_per_item + 0.65 * self.durations[0]
        # Media mobile leggermente pesata verso gli ultimi campioni
        recent = self.durations[-6:]
        return sum(recent) / len(recent)

    def completed_sec(self, now: float) -> float:
        if self.status == "skip":
            return 0.0
        avg = self.avg_sec()
        if self.status == "done":
            eff_n = max(self.total, self.done, 1)
            return eff_n * avg
        base = self.done * avg
        if self.status == "active" and self.item_started_at is not None:
            inflight = min(now - self.item_started_at, avg * 0.92)
            base += max(0.0, inflight)
        return base

    def total_sec(self) -> float:
        if self.status == "skip":
            return 0.0
        eff_n = max(self.total, self.done, 1 if self.status != "pending" or self.total > 0 else 0)
        return eff_n * self.avg_sec()


class ProgressTracker:
    """Calcola percentuale reale ed ETA adattiva lungo tutte le fasi della pipeline."""

    STAGE_ORDER = ("discovery", "transcribe", "mineru", "notes", "figures", "insert", "check", "merge")

    def __init__(self) -> None:
        self.active = False
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.current_phase: str = ""
        self.current_message: str = "Pronto"
        self.notes_done = 0
        self.notes_total = 0
        self.figs_done = 0
        self.figs_total = 0
        self.figs_to_generate = 0
        self.figs_done_generated = 0
        self.phases: dict[str, PhasePlan] = {}
        self.reset_idle()

    def reset_idle(self) -> None:
        self.active = False
        self.started_at = None
        self.finished_at = None
        self.current_phase = ""
        self.current_message = "Pronto"
        self.notes_done = 0
        self.notes_total = 0
        self.figs_done = 0
        self.figs_total = 0
        self.figs_to_generate = 0
        self.figs_done_generated = 0
        self.phases = {
            "discovery": PhasePlan("discovery", prior_sec_per_item=2.0, total=1),
            "transcribe": PhasePlan("transcribe", status="skip", prior_sec_per_item=25.0, total=0),
            "mineru": PhasePlan("mineru", prior_sec_per_item=24.0, total=1),
            "notes": PhasePlan("notes", prior_sec_per_item=35.0, total=1),
            "figures": PhasePlan("figures", prior_sec_per_item=26.0, total=5),
            "insert": PhasePlan("insert", prior_sec_per_item=4.0, total=1),
            "check": PhasePlan("check", prior_sec_per_item=28.0, total=1),
            "merge": PhasePlan("merge", status="skip", prior_sec_per_item=3.0, total=0),
        }

    def start_run(
        self,
        *,
        n_parts: int,
        will_transcribe: bool,
        use_mineru: bool = True,
        skip_notes: bool,
        will_check: bool,
        will_merge: bool,
        merge_only: bool = False,
        known_fig_count: int | None = None,
    ) -> None:
        now = time.monotonic()
        self.active = True
        self.started_at = now
        self.finished_at = None
        self.notes_done = 0
        self.notes_total = 0 if (skip_notes or merge_only) else n_parts
        self.figs_done = 0
        self.figs_total = known_fig_count or 0
        self.figs_to_generate = known_fig_count or 0
        self.figs_done_generated = 0

        if merge_only:
            self.phases = {
                "discovery": PhasePlan("discovery", status="skip", prior_sec_per_item=2.0, total=0),
                "transcribe": PhasePlan("transcribe", status="skip", prior_sec_per_item=25.0, total=0),
                "mineru": PhasePlan("mineru", status="skip", prior_sec_per_item=24.0, total=0),
                "notes": PhasePlan("notes", status="skip", prior_sec_per_item=35.0, total=0),
                "figures": PhasePlan("figures", status="skip", prior_sec_per_item=26.0, total=0),
                "insert": PhasePlan("insert", status="skip", prior_sec_per_item=4.0, total=0),
                "check": PhasePlan("check", status="skip", prior_sec_per_item=28.0, total=0),
                "merge": PhasePlan("merge", status="active", prior_sec_per_item=4.0, total=1, item_started_at=now),
            }
            self.current_phase = "merge"
            self.current_message = "Unione parti del capitolo…"
            return

        est_figs = known_fig_count if known_fig_count is not None else max(3, n_parts * 5)
        self.phases = {
            "discovery": PhasePlan("discovery", status="active", prior_sec_per_item=2.0, total=1, item_started_at=now),
            "transcribe": PhasePlan(
                "transcribe",
                status="pending" if will_transcribe else "skip",
                prior_sec_per_item=25.0,
                total=3 if will_transcribe else 0,
            ),
            "mineru": PhasePlan(
                "mineru",
                status="pending" if use_mineru else "skip",
                prior_sec_per_item=24.0,
                total=max(1, n_parts) if use_mineru else 0,
            ),
            "notes": PhasePlan(
                "notes",
                status="skip" if skip_notes else "pending",
                prior_sec_per_item=30.0 if use_mineru else 45.0,
                total=0 if skip_notes else max(1, n_parts),
                known_exact_total=True,
            ),
            "figures": PhasePlan(
                "figures",
                status="pending",
                prior_sec_per_item=24.0 if use_mineru else 32.0,
                total=est_figs,
                known_exact_total=known_fig_count is not None,
            ),
            "insert": PhasePlan("insert", status="pending", prior_sec_per_item=4.0, total=1, known_exact_total=True),
            "check": PhasePlan(
                "check",
                status="pending" if will_check else "skip",
                prior_sec_per_item=28.0,
                total=1 if will_check else 0,
            ),
            "merge": PhasePlan(
                "merge",
                status="pending" if will_merge else "skip",
                prior_sec_per_item=3.0,
                total=1 if will_merge else 0,
            ),
        }
        self.current_phase = "discovery"
        self.current_message = "Avvio pipeline e analisi sorgenti…"

    def handle_event(self, ev: dict[str, Any]) -> None:
        now = time.monotonic()
        phase_name = ev.get("phase", "")
        status = ev.get("status", "")
        if phase_name not in self.phases:
            return
        ph = self.phases[phase_name]
        self.current_phase = phase_name

        if status == "skip":
            ph.status = "skip"
            ph.total = 0
            ph.done = 0
            return

        if status == "start":
            ph.status = "active"
            ph.total = max(ph.total, 1)
            ph.item_started_at = now
            ph.current_item = ev.get("item", "")
            labels = {
                "discovery": "Analisi cartella capitolo…",
                "insert": "Compilazione e inserimento in main.typ…",
                "merge": "Unione parti in un unico file .typ…",
            }
            self.current_message = ph.current_item or labels.get(phase_name, f"Fase {phase_name}…")
            return

        if status == "plan":
            ph.status = "active"
            ph.known_exact_total = True
            if phase_name == "figures":
                self.figs_total = int(ev.get("total", 0))
                self.figs_to_generate = int(ev.get("to_generate", self.figs_total))
                self.figs_done = 0
                self.figs_done_generated = 0
                ph.total = self.figs_to_generate
                ph.done = 0
                if self.figs_to_generate == 0:
                    self.current_message = "Nessuna figura nuova da generare"
                else:
                    self.current_message = f"Preparazione {self.figs_to_generate} figure CeTZ…"
            elif phase_name == "notes":
                self.notes_total = int(ev.get("total", 0))
                self.notes_done = 0
                ph.total = self.notes_total
                ph.done = 0
                self.current_message = f"Generazione appunti ({self.notes_total} parti)…"
            elif phase_name == "mineru":
                to_p = int(ev.get("to_parse", ev.get("total", 0)))
                ph.total = to_p
                ph.done = 0
                if to_p == 0:
                    self.current_message = "MinerU OCR: tutte le parti già in cache"
                else:
                    self.current_message = f"MinerU OCR + ritaglio figure ({to_p} PDF)…"
            elif phase_name == "transcribe":
                ph.total = int(ev.get("total", 0))
                ph.done = 0
                self.current_message = f"Trascrizione audio ({ph.total} chunk)…"
            elif phase_name == "check":
                ph.total = int(ev.get("total", 2))
                ph.done = 0
                self.current_message = "Avvio controllo visivo PDF…"
            return

        if status == "item_start":
            ph.status = "active"
            ph.item_started_at = now
            item = ev.get("item", "")
            ph.current_item = item
            if phase_name == "mineru":
                self.current_message = f"MinerU OCR — {item}"
            elif phase_name == "notes":
                idx = int(ev.get("done", ph.done)) + 1
                tot = int(ev.get("total", ph.total))
                self.notes_total = tot
                ph.total = tot
                self.current_message = f"Generazione appunti {idx}/{tot} — {item}"
            elif phase_name == "figures":
                idx = int(ev.get("done", self.figs_done)) + 1
                tot = int(ev.get("total", self.figs_total))
                self.figs_total = tot
                self.figs_to_generate = int(ev.get("to_generate", self.figs_to_generate))
                ph.total = max(self.figs_to_generate, 1)
                self.current_message = f"Generazione figura {idx}/{tot} — {item}"
            elif phase_name == "transcribe":
                idx = int(ev.get("done", ph.done)) + 1
                tot = int(ev.get("total", ph.total))
                ph.total = tot
                self.current_message = f"Trascrizione {idx}/{tot} — {item}"
            elif phase_name == "check":
                self.current_message = f"Controllo visivo — {item}"
            return

        if status == "item_retry":
            item = ev.get("item", "")
            att = ev.get("attempt", 1)
            mx = ev.get("max_retries", 2)
            self.current_message = f"Correzione CeTZ {item} (tentativo {att}/{mx})…"
            return

        if status == "item_skip":
            if phase_name == "figures":
                self.figs_done = int(ev.get("done", self.figs_done + 1))
                self.figs_total = int(ev.get("total", self.figs_total))
                self.current_message = f"Figura già presente ({self.figs_done}/{self.figs_total}) — {ev.get('item', '')}"
            return

        if status == "item_done":
            dur = ev.get("duration")
            if isinstance(dur, (int, float)) and dur > 0.2:
                ph.durations.append(float(dur))
            ph.item_started_at = None
            if phase_name == "notes":
                self.notes_done = int(ev.get("done", self.notes_done + 1))
                self.notes_total = int(ev.get("total", self.notes_total))
                ph.done = self.notes_done
                ph.total = self.notes_total
            elif phase_name == "figures":
                self.figs_done = int(ev.get("done", self.figs_done + 1))
                self.figs_total = int(ev.get("total", self.figs_total))
                self.figs_done_generated = int(ev.get("done_generated", self.figs_done_generated + 1))
                self.figs_to_generate = int(ev.get("to_generate", self.figs_to_generate))
                ph.done = self.figs_done_generated
                ph.total = max(self.figs_to_generate, ph.done)
            else:
                ph.done = int(ev.get("done", ph.done + 1))
                ph.total = max(ph.total, int(ev.get("total", ph.total)))
            return

        if status == "done":
            ph.status = "done"
            ph.item_started_at = None
            if phase_name == "figures":
                self.figs_done = int(ev.get("total", self.figs_total))
                self.figs_done_generated = int(ev.get("to_generate", self.figs_to_generate))
                ph.done = ph.total
            else:
                ph.done = max(ph.total, ph.done, 1)
            return

    def finish(self, success: bool) -> None:
        self.active = False
        self.finished_at = time.monotonic()
        if success:
            for ph in self.phases.values():
                if ph.status in ("active", "pending"):
                    ph.status = "done"
            self.current_message = "Completato con successo!"
        else:
            self.current_message = "Esecuzione interrotta o fallita"

    def snapshot(self) -> tuple[float, float | None, float | None]:
        """Restituisce (fraction 0..1, elapsed_sec, eta_remaining_sec)."""
        now = time.monotonic()
        if self.started_at is None:
            return 0.0, None, None
        elapsed = (self.finished_at or now) - self.started_at

        if not self.active:
            done_ok = self.current_message.startswith("Completato")
            return (1.0 if done_ok else self._compute_fraction(now)), elapsed, (0.0 if done_ok else None)

        total_est = sum(ph.total_sec() for ph in self.phases.values())
        comp_est = sum(ph.completed_sec(now) for ph in self.phases.values())
        if total_est <= 0.1:
            return 0.05, elapsed, None

        frac = max(0.02, min(0.99, comp_est / total_est))
        remaining = max(1.0, total_est - comp_est)
        return frac, elapsed, remaining

    def _compute_fraction(self, now: float) -> float:
        total_est = sum(ph.total_sec() for ph in self.phases.values())
        comp_est = sum(ph.completed_sec(now) for ph in self.phases.values())
        if total_est <= 0.1:
            return 0.0
        return max(0.0, min(0.99, comp_est / total_est))


class AppuntiWindow(Adw.ApplicationWindow):
    def __init__(
        self,
        app: Adw.Application,
        init_materia: str | None = None,
        init_cap: int | None = None,
        auto_run: bool = False,
        extra_flags: list[str] | None = None,
        init_tab: str = "appunti",
        config_path: Path | None = None,
    ) -> None:
        super().__init__(application=app, title="Appunti Typst Studio")
        self.set_default_size(1060, 800)
        self.set_size_request(820, 640)
        self.set_icon_name("appunti-typst-studio")

        self.config_path, self.cfg = load_config(config_path)
        self.materie = list(self.cfg.get("materie", {}).keys())
        self.chapters_list: list[tuple[int, str]] = []
        self.current_manifest: dict[str, Any] | None = None
        self.part_checks: dict[str, Gtk.CheckButton] = {}
        self.selected_part_ids: set[str] | None = None
        self.proc: subprocess.Popen[str] | None = None
        self.extra_flags = extra_flags or []
        self.init_parts_filter = self._parse_initial_parts(self.extra_flags)
        self.tracker = ProgressTracker()
        self._suppress_dropdown_cb = False
        self._log_expanded = False
        self._log_line_count = 0
        self._tick_source_id: int | None = None

        self.connect("close-request", self.on_close_request)

        # --- ToastOverlay + ToolbarView ---
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        toolbar_view = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar_view)

        # --- ViewStack unificato (Appunti Pipeline + CeTZ Studio) ---
        self.view_stack = Adw.ViewStack()
        self.view_stack.connect("notify::visible-child-name", self.on_tab_changed)

        # --- HeaderBar con ViewSwitcher al centro ---
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        switcher = Adw.ViewSwitcher(stack=self.view_stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        # Banner intelligente OCR (visibile solo nella scheda Appunti quando serve)
        self.ocr_banner = Adw.Banner(button_label="Attiva Forza OCR")
        self.ocr_banner.set_revealed(False)
        self.ocr_banner.connect("button-clicked", self.on_activate_force_ocr_banner)
        toolbar_view.add_top_bar(self.ocr_banner)

        # Selettori Materia + Capitolo a sinistra (contestuali alla scheda Appunti)
        self.selector_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.pack_start(self.selector_box)

        mat_labels = [
            f"{k.upper()}" if len(k) <= 4 else k.capitalize() for k in self.materie
        ] or ["Nessuna materia"]
        self.materia_dd = Gtk.DropDown.new_from_strings(mat_labels)
        self.materia_dd.set_tooltip_text("Seleziona la materia")
        self.materia_dd.connect("notify::selected", self.on_materia_changed)
        self.selector_box.append(self.materia_dd)

        self.add_mat_btn = Gtk.Button(icon_name="list-add-symbolic")
        self.add_mat_btn.set_tooltip_text("Aggiungi una nuova materia (Wizard)")
        self.add_mat_btn.connect("clicked", lambda *_: self.on_add_subject_clicked())
        self.selector_box.append(self.add_mat_btn)

        self.cap_dd = Gtk.DropDown()
        self.cap_dd.set_enable_search(True)
        self.cap_dd.set_tooltip_text("Seleziona il capitolo o modulo")
        self.cap_dd.set_size_request(200, -1)
        self.cap_dd.connect("notify::selected", self.on_chapter_changed)
        self.selector_box.append(self.cap_dd)

        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        self.refresh_btn.set_tooltip_text("Rianalizza cartella sorgente")
        self.refresh_btn.connect("clicked", lambda *_: self.discover_current(preserve_selection=True))
        self.selector_box.append(self.refresh_btn)

        # Pulsanti rapidi a destra nella HeaderBar
        src_btn = Gtk.Button(icon_name="folder-documents-symbolic")
        src_btn.set_tooltip_text("Apri cartella sorgente del capitolo")
        src_btn.connect("clicked", self.on_open_source)
        header.pack_end(self._build_menu_button())
        localtypst_btn = Gtk.Button(label="LocalTypst")
        localtypst_btn.set_tooltip_text("Apri LocalTypst per vedere il documento live")
        localtypst_btn.connect("clicked", self.on_open_localtypst)
        header.pack_end(localtypst_btn)
        proj_btn = Gtk.Button(icon_name="folder-open-symbolic")
        proj_btn.set_tooltip_text("Apri cartella progetto Typst locale")
        proj_btn.connect("clicked", self.on_open_project)
        header.pack_end(proj_btn)
        header.pack_end(src_btn)

        # --- Scheda 1: Appunti Pipeline ---
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        clamp = Adw.Clamp(maximum_size=920, tightening_threshold=680)
        clamp.set_margin_top(16)
        clamp.set_margin_bottom(20)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)
        scrolled.set_child(clamp)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(main_box)

        main_box.append(self._build_hero_card())
        main_box.append(self._build_parts_section())
        main_box.append(self._build_chapter_figures_section())
        main_box.append(self._build_options_section())
        main_box.append(self._build_log_card())

        # Drag & Drop di .txt / audio / video sulla scheda Appunti
        drop_app = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_app.connect("drop", self.on_appunti_files_dropped)
        scrolled.add_controller(drop_app)

        page_appunti = self.view_stack.add_titled(scrolled, "appunti", "Appunti")
        page_appunti.set_icon_name("accessories-text-editor-symbolic")

        # --- Scheda 2: CeTZ Studio ---
        self.cetz_view = CeTZStudioView(
            window=self,
            general_cfg=self.cfg["general"],
            toast_cb=self.show_toast,
        )
        page_cetz = self.view_stack.add_titled(self.cetz_view, "cetz", "CeTZ Studio")
        page_cetz.set_icon_name("applications-graphics-symbolic")

        toolbar_view.set_content(self.view_stack)

        # --- Inizializzazione materia & capitolo ---
        if self.materie:
            self._suppress_dropdown_cb = True
            if init_materia and init_materia.lower() in self.materie:
                self.materia_dd.set_selected(self.materie.index(init_materia.lower()))
            self._populate_chapters(prefer_cap=init_cap if init_cap is not None else 12)
            self._suppress_dropdown_cb = False
            self.discover_current(preserve_selection=False, apply_initial_filter=True)

        if init_tab == "cetz":
            self.view_stack.set_visible_child_name("cetz")
        self.on_tab_changed()

        if auto_run and init_tab != "cetz" and init_materia and init_cap is not None:
            if self.run_btn.get_sensitive():
                GLib.idle_add(self.on_run_clicked)
            else:
                missing = (
                    self.current_manifest["missing"]["parts_without_txt"]
                    if self.current_manifest
                    else []
                )
                send_notify(
                    "Appunti: azione richiesta",
                    f"Mancano i .txt per le parti {missing}. Associali nella finestra aperta.",
                    urgency="critical",
                )

    def on_tab_changed(self, *_: Any) -> None:
        is_appunti = self.view_stack.get_visible_child_name() == "appunti"
        self.selector_box.set_visible(is_appunti)
        if not is_appunti:
            self.ocr_banner.set_revealed(False)
        else:
            self.refresh_action_buttons()

    @staticmethod
    def _parse_initial_parts(flags: list[str]) -> set[str] | None:
        out: set[str] = set()
        for f in flags:
            if f.startswith("--part="):
                val = f.split("=", 1)[1]
                for tok in val.split(","):
                    if tok.strip():
                        out.add(tok.strip().lower())
        return out or None

    def _build_menu_button(self) -> Gtk.MenuButton:
        menu = Gio.Menu()
        sec0 = Gio.Menu()
        sec0.append("Nuova materia…", "win.add-subject")
        menu.append_section(None, sec0)

        sec1 = Gio.Menu()
        sec1.append("Apri cartella Prompt", "win.open-prompts")
        sec1.append("Apri config.toml", "win.open-config")
        menu.append_section(None, sec1)

        sec2 = Gio.Menu()
        sec2.append("Pulisci file generati del capitolo…", "win.clean-chapter")
        menu.append_section(None, sec2)

        act_add = Gio.SimpleAction.new("add-subject", None)
        act_add.connect("activate", lambda *_: self.on_add_subject_clicked())
        self.add_action(act_add)

        act_prompts = Gio.SimpleAction.new("open-prompts", None)
        act_prompts.connect("activate", lambda *_: subprocess.Popen(["xdg-open", str(self.cfg["general"]["prompts_dir"])]))
        self.add_action(act_prompts)

        act_cfg = Gio.SimpleAction.new("open-config", None)
        act_cfg.connect("activate", lambda *_: subprocess.Popen(["xdg-open", str(self.config_path)]))
        self.add_action(act_cfg)

        act_clean = Gio.SimpleAction.new("clean-chapter", None)
        act_clean.connect("activate", lambda *_: self.on_clean_chapter_clicked())
        self.add_action(act_clean)

        btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        btn.set_tooltip_text("Altre azioni")
        return btn

    def _build_hero_card(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        card.add_css_class("card")
        card.add_css_class("hero-card")

        # Riga titolo capitolo + pulsanti azione principali
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        top.set_valign(Gtk.Align.CENTER)
        card.append(top)

        title_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        title_col.set_hexpand(True)
        top.append(title_col)

        self.chapter_heading = Gtk.Label(label="Seleziona un capitolo", xalign=0)
        self.chapter_heading.add_css_class("title-2")
        self.chapter_heading.set_ellipsize(Pango.EllipsizeMode.END)
        title_col.append(self.chapter_heading)

        self.meta_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title_col.append(self.meta_box)

        # Pulsanti azione
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_valign(Gtk.Align.CENTER)
        top.append(actions)

        self.run_btn = Gtk.Button()
        self.run_btn.add_css_class("suggested-action")
        self.run_btn.add_css_class("pill")
        self.run_btn.set_sensitive(False)
        self.run_btn_content = Adw.ButtonContent(
            icon_name="media-playback-start-symbolic",
            label="Genera",
        )
        self.run_btn.set_child(self.run_btn_content)
        self.run_btn.connect("clicked", self.on_run_clicked)
        actions.append(self.run_btn)

        self.stop_btn = Gtk.Button()
        self.stop_btn.add_css_class("destructive-action")
        self.stop_btn.add_css_class("pill")
        self.stop_btn.set_visible(False)
        self.stop_btn.set_child(
            Adw.ButtonContent(icon_name="process-stop-symbolic", label="Interrompi")
        )
        self.stop_btn.connect("clicked", self.on_stop_clicked)
        actions.append(self.stop_btn)

        self.merge_btn = Gtk.Button()
        self.merge_btn.add_css_class("pill")
        self.merge_btn.set_sensitive(False)
        self.merge_btn.set_tooltip_text("Unisce i file _parts/<capitolo>/p*.typ in un unico file capitolo")
        self.merge_btn.set_child(
            Adw.ButtonContent(icon_name="document-save-symbolic", label="Unisci")
        )
        self.merge_btn.connect("clicked", self.on_merge_clicked)
        actions.append(self.merge_btn)

        card.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Stepper delle fasi
        stepper = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        stepper.set_halign(Gtk.Align.START)
        card.append(stepper)

        self.stage_labels: dict[str, Gtk.Label] = {}
        stage_defs = [
            ("discovery", "1. Discovery"),
            ("mineru", "2. MinerU OCR"),
            ("transcribe", "3. Trascrivi"),
            ("notes", "4. Appunti"),
            ("figures", "5. Figure CeTZ"),
            ("insert", "6. Compila"),
            ("check", "7. Check"),
        ]
        for key, text in stage_defs:
            lbl = Gtk.Label(label=text)
            lbl.add_css_class("stage-pill")
            stepper.append(lbl)
            self.stage_labels[key] = lbl

        # Stato corrente + percentuale
        prog_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        card.append(prog_head)

        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        prog_head.append(self.spinner)

        self.activity_label = Gtk.Label(label="Pronto alla generazione", xalign=0)
        self.activity_label.set_hexpand(True)
        self.activity_label.set_ellipsize(Pango.EllipsizeMode.END)
        prog_head.append(self.activity_label)

        self.percent_label = Gtk.Label(label="0%")
        self.percent_label.add_css_class("percent-label")
        prog_head.append(self.percent_label)

        # Barra dei progressi effettiva
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.add_css_class("appunti-progress")
        self.progress_bar.set_fraction(0.0)
        card.append(self.progress_bar)

        # Riga metriche: Trascorso | ETA | Contatori parti/figure
        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        card.append(stats)

        self.elapsed_label = Gtk.Label(label="Trascorso: —")
        self.elapsed_label.add_css_class("stat-pill")
        stats.append(self.elapsed_label)

        self.eta_label = Gtk.Label(label="Stimato: —")
        self.eta_label.add_css_class("stat-pill")
        stats.append(self.eta_label)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        stats.append(spacer)

        self.counts_label = Gtk.Label(label="Parti: —  ·  Figure: —")
        self.counts_label.add_css_class("stat-pill")
        stats.append(self.counts_label)

        return card

    def _build_parts_section(self) -> Gtk.Widget:
        self.parts_group = Adw.PreferencesGroup(
            title="Parti del capitolo",
            description="Seleziona le parti che vuoi elaborare",
        )

        # Controlli rapidi di selezione parti nell'header della sezione
        sel_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        sel_controls.add_css_class("linked")
        sel_controls.set_valign(Gtk.Align.CENTER)

        btn_all = Gtk.Button(label="Tutte")
        btn_all.connect("clicked", lambda *_: self.select_parts_preset("all"))
        btn_ready = Gtk.Button(label="Pronte")
        btn_ready.connect("clicked", lambda *_: self.select_parts_preset("ready"))
        btn_none = Gtk.Button(label="Nessuna")
        btn_none.connect("clicked", lambda *_: self.select_parts_preset("none"))

        for b in (btn_all, btn_ready, btn_none):
            sel_controls.append(b)
        self.parts_group.set_header_suffix(sel_controls)

        self.parts_listbox = Gtk.ListBox()
        self.parts_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.parts_listbox.add_css_class("boxed-list")
        self.parts_group.add(self.parts_listbox)

        return self.parts_group

    def _build_chapter_figures_section(self) -> Gtk.Widget:
        self.figs_group = Adw.PreferencesGroup()
        self.figs_group.set_visible(False)
        lb = Gtk.ListBox()
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        self.figs_group.add(lb)

        self.figs_expander = Adw.ExpanderRow(
            title="Figure del capitolo",
            subtitle="Apri una figura direttamente in CeTZ Studio per rifinirla live",
        )
        self.figs_expander.add_prefix(Gtk.Image.new_from_icon_name("applications-graphics-symbolic"))
        lb.append(self.figs_expander)
        self._fig_rows: list[Gtk.Widget] = []
        return self.figs_group

    def _build_options_section(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup()
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        group.add(listbox)

        self.opts_expander = Adw.ExpanderRow(
            title="Opzioni pipeline",
            subtitle="MinerU OCR · Figure: Ibrida · Check visivo",
        )
        self.opts_expander.add_prefix(Gtk.Image.new_from_icon_name("emblem-system-symbolic"))
        listbox.append(self.opts_expander)

        # Modalita figure: Ibrida / Tutto CeTZ / Solo ritagli MinerU
        self.combo_fig_mode = Adw.ComboRow(
            title="Modalità figure",
            subtitle="Ibrida: CeTZ per schemi/grafici, ritaglio MinerU istantaneo per foto e sezioni reali",
            model=Gtk.StringList.new([lbl for _, lbl in FIG_MODES]),
        )
        def_fm = self.cfg["general"].get("figures_mode", "hybrid")
        for f in self.extra_flags:
            if f.startswith("--figures-mode="):
                def_fm = f.split("=", 1)[1].strip()
        fm_ids = [k for k, _ in FIG_MODES]
        self.combo_fig_mode.set_selected(fm_ids.index(def_fm) if def_fm in fm_ids else 0)
        self.combo_fig_mode.connect("notify::selected", self.on_options_changed)
        self.opts_expander.add_row(self.combo_fig_mode)

        # Backend LLM: agy CLI vs Gemini REST API diretta
        self.combo_backend = Adw.ComboRow(
            title="Backend AI (LLM)",
            subtitle="Usa Antigravity CLI (agy) oppure Gemini REST API diretta con GEMINI_API_KEY",
            model=Gtk.StringList.new([lbl for _, lbl in LLM_BACKENDS]),
        )
        def_bk = self.cfg["general"].get("llm_backend", "agy")
        for f in self.extra_flags:
            if f.startswith("--backend="):
                def_bk = f.split("=", 1)[1].strip()
        bk_ids = [k for k, _ in LLM_BACKENDS]
        self.combo_backend.set_selected(bk_ids.index(def_bk) if def_bk in bk_ids else 0)
        self.combo_backend.connect("notify::selected", self.on_options_changed)
        self.opts_expander.add_row(self.combo_backend)

        self.sw_mineru = Adw.SwitchRow(
            title="Usa MinerU OCR + ritaglio figure (--mineru)",
            subtitle="Estrae Markdown OCR, formule e ritagli puliti dal PDF (spegni per usare il PDF crudo + pdftoppm)",
        )
        default_mineru = bool(self.cfg["general"].get("use_mineru_default", True))
        if "--no-mineru" in self.extra_flags:
            default_mineru = False
        elif "--mineru" in self.extra_flags:
            default_mineru = True
        self.sw_mineru.set_active(default_mineru)
        self.sw_mineru.connect("notify::active", self.on_options_changed)
        self.opts_expander.add_row(self.sw_mineru)

        self.sw_force_ocr = Adw.SwitchRow(
            title="Forza OCR completo (--force-ocr)",
            subtitle="Rilegge visivamente ogni pagina con OCR (utile per PDF scansionati o font/formule corrotti)",
        )
        self.sw_force_ocr.set_active("--force-ocr" in self.extra_flags)
        self.sw_force_ocr.set_sensitive(default_mineru)
        self.sw_force_ocr.connect("notify::active", self.on_options_changed)
        self.opts_expander.add_row(self.sw_force_ocr)

        self.sw_visual = Adw.SwitchRow(
            title="Controllo visivo finale (--check)",
            subtitle="Compila il capitolo in PDF e controlla overflow e sovrapposizioni",
        )
        self.sw_visual.set_active("--no-check" not in self.extra_flags)
        self.sw_visual.connect("notify::active", self.on_options_changed)
        self.opts_expander.add_row(self.sw_visual)

        self.sw_transcribe = Adw.SwitchRow(
            title="Trascrivi audio/video con Gemini API (--transcribe)",
            subtitle="Genera automaticamente i .txt mancanti se è presente un video o audio",
        )
        self.sw_transcribe.set_active("--transcribe" in self.extra_flags)
        self.sw_transcribe.connect("notify::active", self.on_options_changed)
        self.opts_expander.add_row(self.sw_transcribe)

        self.sw_force_figs = Adw.SwitchRow(
            title="Rigenera figure già esistenti (--force-figures)",
            subtitle="Ricrea i file CeTZ in figs/ anche se già presenti",
        )
        self.sw_force_figs.set_active("--force-figures" in self.extra_flags)
        self.sw_force_figs.connect("notify::active", self.on_options_changed)
        self.opts_expander.add_row(self.sw_force_figs)

        self.sw_skip_notes = Adw.SwitchRow(
            title="Solo figure, salta generazione testo (--skip-notes)",
            subtitle="Usa i file p*.typ già generati per creare o rigenerare solo le figure CeTZ",
        )
        self.sw_skip_notes.set_active("--skip-notes" in self.extra_flags)
        self.sw_skip_notes.connect("notify::active", self.on_options_changed)
        self.opts_expander.add_row(self.sw_skip_notes)

        self.sw_merge = Adw.SwitchRow(
            title="Unisci automaticamente le parti al termine (--merge)",
            subtitle="Fonde i file generati in _parts/ in un unico file capitolo .typ",
        )
        self.sw_merge.set_active("--merge" in self.extra_flags)
        self.sw_merge.connect("notify::active", self.on_options_changed)
        self.opts_expander.add_row(self.sw_merge)

        self._update_options_summary()
        return group

    def _build_log_card(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("card")
        card.add_css_class("log-card")
        card.set_overflow(Gtk.Overflow.HIDDEN)

        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hdr.add_css_class("log-header")
        card.append(hdr)

        hdr.append(Gtk.Image.new_from_icon_name("utilities-terminal-symbolic"))
        lbl = Gtk.Label(label="Log esecuzione", xalign=0)
        lbl.add_css_class("heading")
        hdr.append(lbl)

        self.log_count_chip = Gtk.Label(label="0 righe")
        self.log_count_chip.add_css_class("meta-chip")
        hdr.append(self.log_count_chip)

        sp = Gtk.Box()
        sp.set_hexpand(True)
        hdr.append(sp)

        self.log_expand_btn = Gtk.Button(label="Espandi")
        self.log_expand_btn.add_css_class("flat")
        self.log_expand_btn.connect("clicked", self.on_toggle_log_size)
        hdr.append(self.log_expand_btn)

        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        copy_btn.add_css_class("flat")
        copy_btn.set_tooltip_text("Copia log negli appunti")
        copy_btn.connect("clicked", self.on_copy_log)
        hdr.append(copy_btn)

        clear_btn = Gtk.Button(icon_name="edit-clear-symbolic")
        clear_btn.add_css_class("flat")
        clear_btn.set_tooltip_text("Pulisci log")
        clear_btn.connect("clicked", self.on_clear_log)
        hdr.append(clear_btn)

        self.log_scroll = Gtk.ScrolledWindow()
        self.log_scroll.set_min_content_height(210)
        self.log_scroll.set_max_content_height(210)
        self.log_scroll.set_vexpand(True)
        card.append(self.log_scroll)

        self.log_view = Gtk.TextView()
        self.log_view.add_css_class("log-view")
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_buf = self.log_view.get_buffer()
        self.log_scroll.set_child(self.log_view)

        # Tag semantici per colorare il log in modo leggibile
        self.log_buf.create_tag("phase", weight=Pango.Weight.BOLD, foreground="#3584e4")
        self.log_buf.create_tag("ok", foreground="#2ec27e", weight=Pango.Weight.SEMIBOLD)
        self.log_buf.create_tag("warn", foreground="#e5a50a", weight=Pango.Weight.SEMIBOLD)
        self.log_buf.create_tag("err", foreground="#e01b24", weight=Pango.Weight.BOLD)
        self.log_buf.create_tag("cmd", foreground="#77767b")

        return card

    # --- Helpers stato corrente ---

    def current_materia(self) -> str:
        idx = self.materia_dd.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.materie):
            return self.materie[0]
        return self.materie[idx]

    def current_chapter_num(self) -> int | None:
        idx = self.cap_dd.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.chapters_list):
            return None
        return self.chapters_list[idx][0]

    def show_toast(self, message: str, timeout: int = 3) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(timeout)
        self.toast_overlay.add_toast(toast)

    def _populate_chapters(self, prefer_cap: int | None = None) -> None:
        mat = self.current_materia()
        self.chapters_list = discover_available_chapters(self.cfg["materie"][mat])
        labels = [f"{num:02d} · {title}" for num, title in self.chapters_list]
        if not labels:
            labels = ["Nessun capitolo trovato"]
        model = Gtk.StringList.new(labels)
        self.cap_dd.set_model(model)

        if self.chapters_list:
            target_idx = 0
            if prefer_cap is not None:
                for i, (num, _) in enumerate(self.chapters_list):
                    if num == prefer_cap:
                        target_idx = i
                        break
            else:
                target_idx = len(self.chapters_list) - 1
            self.cap_dd.set_selected(target_idx)

    # --- Eventi UI ---

    def on_materia_changed(self, *_: Any) -> None:
        if self._suppress_dropdown_cb:
            return
        self._suppress_dropdown_cb = True
        self._populate_chapters(prefer_cap=None)
        self._suppress_dropdown_cb = False
        self.discover_current(preserve_selection=False)

    def on_chapter_changed(self, *_: Any) -> None:
        if self._suppress_dropdown_cb:
            return
        self.discover_current(preserve_selection=False)

    def on_options_changed(self, *_: Any) -> None:
        self.sw_force_ocr.set_sensitive(self.sw_mineru.get_active())
        self._update_options_summary()
        self.refresh_action_buttons()

    def on_activate_force_ocr_banner(self, *_: Any) -> None:
        self.sw_mineru.set_active(True)
        self.sw_force_ocr.set_active(True)
        self.show_toast("Forza OCR attivato (--force-ocr)")

    def current_figures_mode(self) -> str:
        idx = self.combo_fig_mode.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(FIG_MODES):
            return "hybrid"
        return FIG_MODES[idx][0]

    def current_backend(self) -> str:
        idx = self.combo_backend.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(LLM_BACKENDS):
            return "agy"
        return LLM_BACKENDS[idx][0]

    def _update_options_summary(self) -> None:
        if self.sw_mineru.get_active():
            parser_str = "MinerU (Forza OCR)" if self.sw_force_ocr.get_active() else "MinerU OCR"
        else:
            parser_str = "PDF crudo"
        fm_map = {"hybrid": "Figure: Ibrida", "cetz": "Figure: Tutto CeTZ", "mineru": "Figure: Solo ritagli"}
        active: list[str] = [parser_str, fm_map.get(self.current_figures_mode(), "Figure: Ibrida")]
        if self.current_backend() == "gemini-api":
            active.append("Gemini REST API")
        if self.sw_visual.get_active():
            active.append("Check visivo")
        if self.sw_transcribe.get_active():
            active.append("Trascrizione API")
        if self.sw_force_figs.get_active():
            active.append("Rigenera figure")
        if self.sw_skip_notes.get_active():
            active.append("Solo figure")
        if self.sw_merge.get_active():
            active.append("Merge automatico")
        self.opts_expander.set_subtitle(" · ".join(active))

    def on_toggle_log_size(self, *_: Any) -> None:
        self._log_expanded = not self._log_expanded
        h = 430 if self._log_expanded else 210
        self.log_scroll.set_min_content_height(h)
        self.log_scroll.set_max_content_height(h)
        self.log_expand_btn.set_label("Riduci" if self._log_expanded else "Espandi")

    def on_copy_log(self, *_: Any) -> None:
        start = self.log_buf.get_start_iter()
        end = self.log_buf.get_end_iter()
        text = self.log_buf.get_text(start, end, True)
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(text)
        self.show_toast("Log copiato negli appunti")

    def on_clear_log(self, *_: Any) -> None:
        self.log_buf.set_text("")
        self._log_line_count = 0
        self.log_count_chip.set_label("0 righe")

    def on_open_localtypst(self, *_: Any) -> None:
        if shutil.which("localtypst"):
            subprocess.Popen(["localtypst"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.show_toast("Apertura di LocalTypst…")
        else:
            self.show_toast("LocalTypst non trovato nel PATH")

    def on_open_project(self, *_: Any) -> None:
        mat = self.current_materia()
        p = Path(self.cfg["materie"][mat]["typst_project_dir"]).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(p)])

    def on_open_source(self, *_: Any) -> None:
        if self.current_manifest and self.current_manifest.get("chapter_dir"):
            p = Path(self.current_manifest["chapter_dir"])
        else:
            p = Path(self.cfg["materie"][self.current_materia()]["source_dir"]).expanduser()
        if p.exists():
            subprocess.Popen(["xdg-open", str(p)])

    # --- Discovery & UI Parti ---

    def discover_current(
        self,
        preserve_selection: bool = False,
        apply_initial_filter: bool = False,
    ) -> None:
        mat = self.current_materia()
        cap = self.current_chapter_num()
        if cap is None:
            self.current_manifest = None
            self.refresh_parts_ui()
            return

        prev_sel = set(self.get_selected_part_ids()) if preserve_selection else None
        work_dir = Path(self.cfg["general"]["work_dir"]).expanduser()
        manifest_path = work_dir / f"{mat}_cap{cap}_manifest.json"
        res = subprocess.run(
            [
                sys.executable,
                str(CORE_DIR / "discover.py"),
                mat,
                str(cap),
                "--config",
                str(self.config_path),
                "--out",
                str(manifest_path),
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode not in (0, 2):
            self.append_log_line(res.stderr or res.stdout)

        if manifest_path.exists():
            self.current_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            self.current_manifest = None

        if apply_initial_filter and self.init_parts_filter is not None:
            self.selected_part_ids = set(self.init_parts_filter)
            self.init_parts_filter = None
        elif prev_sel is not None:
            self.selected_part_ids = prev_sel
        else:
            self.selected_part_ids = None

        self.refresh_parts_ui()
        if not self.tracker.active:
            self.tracker.reset_idle()
            self._render_progress_ui()

    def _clear_box(self, box: Gtk.Widget) -> None:
        while child := box.get_first_child():
            box.remove(child)

    def _set_meta_chips(self, chips: list[str]) -> None:
        self._clear_box(self.meta_box)
        for text in chips:
            lbl = Gtk.Label(label=text)
            lbl.add_css_class("meta-chip")
            self.meta_box.append(lbl)

    def _part_generated_info(self, m: dict[str, Any], part_id: str) -> tuple[bool, Path, int]:
        slug = m.get("chapter_slug", f"cap{m['capitolo']}")
        pfile = Path(m["typst_project_dir"]) / "_parts" / slug / f"p{part_id}.typ"
        if not pfile.exists():
            return False, pfile, 0
        try:
            n_flags = len(FLAG_COUNT_RX.findall(pfile.read_text(encoding="utf-8")))
        except Exception:
            n_flags = 0
        return True, pfile, n_flags

    def refresh_parts_ui(self) -> None:
        self._clear_box(self.parts_listbox)
        self.part_checks.clear()

        m = self.current_manifest
        if not m or m["missing"]["chapter_not_found"]:
            self.chapter_heading.set_text("Nessun capitolo trovato")
            self._set_meta_chips([])
            empty = Adw.ActionRow(
                title="Nessuna parte disponibile",
                subtitle="Controlla la cartella sorgente della materia selezionata.",
            )
            self.parts_listbox.append(empty)
            self.refresh_action_buttons()
            return

        slug = m.get("chapter_slug", f"cap{m['capitolo']}")
        self.chapter_heading.set_text(f"Capitolo {m['capitolo']} — {m['chapter_title']}")

        typst_dir = Path(m["typst_project_dir"])
        cap_exists = (typst_dir / f"{slug}.typ").exists()
        figs_dir = typst_dir / m["figs_dir"]
        existing_figs = (
            len(list(figs_dir.glob(f"{m['materia']}-c{m['capitolo']}-*.typ")))
            if figs_dir.exists()
            else 0
        )

        chips = [
            f"{slug}.typ" + (" ✓" if cap_exists else ""),
            f"{len(m['parts'])} parti",
            f"{existing_figs} figure CeTZ" if existing_figs else "Copia locale sicura",
        ]
        self._set_meta_chips(chips)

        if not m["parts"]:
            empty = Adw.ActionRow(
                title="Cartella vuota",
                subtitle=f"Nessun PDF trovato in {m.get('chapter_dir', m['source_dir'])}",
            )
            self.parts_listbox.append(empty)
            self.refresh_action_buttons()
            return

        for part in m["parts"]:
            pid = part["part_id"]
            has_pdf = bool(part.get("pdf"))
            has_txt = bool(part.get("txt"))
            has_media = bool(part.get("media"))
            is_gen, pfile, n_flags = self._part_generated_info(m, pid)

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row_box.add_css_class("part-row-box")
            row_box.set_valign(Gtk.Align.CENTER)

            # Checkbox selezione parte
            chk = Gtk.CheckButton()
            chk.set_valign(Gtk.Align.CENTER)
            if not has_pdf:
                chk.set_sensitive(False)
                chk.set_active(False)
            else:
                if self.selected_part_ids is not None:
                    chk.set_active(pid.lower() in self.selected_part_ids)
                else:
                    # Di default seleziona tutte le parti pronte (o trascrivibili se --transcribe è attivo)
                    default_on = has_txt or (part["needs_transcription"] and self.sw_transcribe.get_active())
                    chk.set_active(default_on)
            chk.connect("toggled", lambda *_: self.refresh_action_buttons())
            self.part_checks[pid] = chk
            row_box.append(chk)

            # Colonna testo (Titolo parte + file abbinati)
            info_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            info_col.set_hexpand(True)
            row_box.append(info_col)

            pdf_name = Path(part["pdf"]).name if has_pdf else "PDF mancante"
            title_lbl = Gtk.Label(xalign=0)
            title_lbl.set_markup(
                f"<b>Parte {GLib.markup_escape_text(str(pid))}</b>  "
                f"<span alpha='68%'>{GLib.markup_escape_text(pdf_name)}</span>"
            )
            title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            info_col.append(title_lbl)

            sub_bits: list[str] = []
            if has_txt:
                sub_bits.append(f"TXT: {Path(part['txt']).name}")
            elif has_media:
                sub_bits.append(f"Media: {Path(part['media']).name}")
            else:
                sub_bits.append("Nessuna trascrizione .txt abbinata")
            ocr_info = part.get("ocr_info") or {}
            ocr_level = ocr_info.get("level", "")
            if ocr_info.get("pages"):
                if ocr_level == "ok":
                    sub_bits.append(f"{ocr_info['pages']} pag. · testo OK")
                elif ocr_info.get("reason"):
                    sub_bits.append(ocr_info["reason"])
            if is_gen:
                flag_txt = f" ({n_flags} figure)" if n_flags else ""
                sub_bits.append(f"p{pid}.typ presente{flag_txt}")

            sub_lbl = Gtk.Label(label="  ·  ".join(sub_bits), xalign=0)
            sub_lbl.add_css_class("caption")
            sub_lbl.add_css_class("dim-label")
            sub_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            info_col.append(sub_lbl)

            # Badge avviso OCR per parte (solo se necessario o consigliato)
            if ocr_level in ("required", "recommended"):
                ocr_badge = Gtk.Label(label=ocr_info.get("label", "OCR consigliato"))
                ocr_badge.add_css_class("status-badge")
                ocr_badge.add_css_class("badge-err" if ocr_level == "required" else "badge-warn")
                ocr_badge.set_tooltip_text(ocr_info.get("reason", "Consigliato attivare Forza OCR"))
                ocr_badge.set_valign(Gtk.Align.CENTER)
                row_box.append(ocr_badge)

            # Badge stato colorato
            if not has_pdf:
                badge_txt, badge_cls = "Manca PDF", "badge-err"
            elif is_gen:
                badge_txt, badge_cls = "Generata", "badge-generated"
            elif has_txt:
                badge_txt, badge_cls = "Pronta", "badge-ready"
            elif part["needs_transcription"]:
                badge_txt, badge_cls = "Da trascrivere", "badge-warn"
            else:
                badge_txt, badge_cls = "Manca .txt", "badge-err"

            badge = Gtk.Label(label=badge_txt)
            badge.add_css_class("status-badge")
            badge.add_css_class(badge_cls)
            badge.set_valign(Gtk.Align.CENTER)
            row_box.append(badge)

            # Azioni contestuali per riga
            if not has_txt and m.get("chapter_dir"):
                attach_btn = Gtk.Button(label="Associa .txt / Audio")
                attach_btn.set_valign(Gtk.Align.CENTER)
                attach_btn.connect("clicked", lambda *_, p=part: self.on_attach_file(p))
                row_box.append(attach_btn)
            elif is_gen:
                open_part_btn = Gtk.Button(icon_name="document-edit-symbolic")
                open_part_btn.add_css_class("flat")
                open_part_btn.set_valign(Gtk.Align.CENTER)
                open_part_btn.set_tooltip_text(f"Apri {pfile.name}")
                open_part_btn.connect("clicked", lambda *_, f=pfile: subprocess.Popen(["xdg-open", str(f)]))
                row_box.append(open_part_btn)

            list_row = Gtk.ListBoxRow()
            list_row.set_activatable(False)
            list_row.set_child(row_box)
            self.parts_listbox.append(list_row)

        self.refresh_chapter_figures_ui()
        self.refresh_action_buttons()

    def _find_orig_crop_for_fig(self, m: dict[str, Any], fig_id: str) -> Path | None:
        slug = m.get("chapter_slug", f"cap{m['capitolo']}")
        parts_dir = Path(m["typst_project_dir"]) / "_parts" / slug
        work_dir = Path(self.cfg["general"]["work_dir"]).expanduser()
        cache_root = work_dir / "mineru_cache" / f"{m['materia']}_cap{m['capitolo']}"
        if not cache_root.exists():
            return None
        # Cerca il parametro src: nel #img-flag corrispondente
        scan_files = list(parts_dir.glob("p*.typ")) if parts_dir.exists() else []
        cap_file = Path(m["typst_project_dir"]) / f"{slug}.typ"
        if cap_file.exists():
            scan_files.append(cap_file)
        for tf in scan_files:
            try:
                txt = tf.read_text(encoding="utf-8")
            except Exception:
                continue
            for fm in FLAG_SRC_LOOKUP_RX.finditer(txt):
                if fm.group(1).strip() != fig_id:
                    continue
                sm = SRC_PARAM_RX.search(fm.group(2))
                if sm:
                    fname = Path(sm.group(1).strip()).name
                    for pdir in cache_root.glob("p*"):
                        cand = pdir / "images" / fname
                        if cand.exists():
                            return cand
        return None

    def refresh_chapter_figures_ui(self) -> None:
        for r in self._fig_rows:
            self.figs_expander.remove(r)
        self._fig_rows.clear()

        m = self.current_manifest
        if not m or m["missing"]["chapter_not_found"]:
            self.figs_group.set_visible(False)
            return

        figs_dir = Path(m["typst_project_dir"]) / m["figs_dir"]
        prefix = f"{m['materia']}-c{m['capitolo']}-"
        found: dict[str, Path] = {}
        if figs_dir.exists():
            for p in sorted(figs_dir.iterdir()):
                if not p.is_file() or not p.name.startswith(prefix):
                    continue
                if p.suffix == ".typ":
                    found[p.stem] = p
                elif p.suffix.lower() in (".png", ".jpg", ".jpeg") and p.stem not in found:
                    found[p.stem] = p

        if not found:
            self.figs_group.set_visible(False)
            return

        self.figs_group.set_visible(True)
        self.figs_expander.set_title(f"Figure del capitolo ({len(found)})")
        self.figs_expander.set_subtitle("Clicca su una figura per rifinirla in CeTZ Studio con aggiornamento live")

        for fid, fpath in sorted(found.items()):
            is_cetz = fpath.suffix == ".typ"
            row = Adw.ActionRow(
                title=fid,
                subtitle="Vettoriale CeTZ (.typ)" if is_cetz else f"Ritaglio immagine ({fpath.name})",
            )
            badge = Gtk.Label(label="CeTZ" if is_cetz else "Ritaglio")
            badge.add_css_class("status-badge")
            badge.add_css_class("badge-ready" if is_cetz else "badge-generated")
            badge.set_valign(Gtk.Align.CENTER)
            row.add_suffix(badge)

            btn_edit = Gtk.Button(label="Modifica in CeTZ Studio")
            btn_edit.set_valign(Gtk.Align.CENTER)
            btn_edit.connect(
                "clicked",
                lambda *_, _fid=fid, _fp=fpath, _fd=figs_dir: self._open_fig_in_studio(_fid, _fp, _fd),
            )
            row.add_suffix(btn_edit)
            self.figs_expander.add_row(row)
            self._fig_rows.append(row)

    def _open_fig_in_studio(self, fig_id: str, fig_path: Path, figs_dir: Path) -> None:
        orig = self._find_orig_crop_for_fig(self.current_manifest, fig_id) if self.current_manifest else None
        self.view_stack.set_visible_child_name("cetz")
        self.cetz_view.open_project_figure(
            fig_id=fig_id,
            fig_path=fig_path,
            figs_dir=figs_dir,
            orig_img=orig,
            on_saved=lambda: GLib.idle_add(self.refresh_chapter_figures_ui),
        )

    def on_appunti_files_dropped(self, _target: Gtk.DropTarget, value: Any, _x: float, _y: float) -> bool:
        if self.proc is not None or not self.current_manifest or not isinstance(value, Gdk.FileList):
            return False
        chap_dir_str = self.current_manifest.get("chapter_dir")
        if not chap_dir_str:
            return False
        chap_dir = Path(chap_dir_str)
        files = [Path(f.get_path()) for f in value.get_files() if f.get_path()]
        supported = [
            f
            for f in files
            if f.suffix.lower() in (".txt", ".mp4", ".mkv", ".webm", ".mov", ".mp3", ".wav", ".m4a", ".ogg")
        ]
        if not supported:
            self.show_toast("Trascina file .txt oppure audio/video per associarli al capitolo")
            return False

        missing_parts = list(self.current_manifest["missing"]["parts_without_txt"])
        mat = self.current_manifest["materia"].upper()
        cap = self.current_manifest["capitolo"]
        copied = 0
        for src in supported:
            # Se il nome file contiene già il pattern del capitolo, copialo direttamente; altrimenti abbinalo alla prima parte senza .txt
            if re.search(rf"{mat}\s*0?{cap}[-_]\d+", src.name, re.IGNORECASE):
                dst = chap_dir / src.name
            elif missing_parts:
                pid = missing_parts.pop(0)
                dst = chap_dir / f"{mat}{cap:02d}_{pid}{src.suffix.lower()}"
            else:
                dst = chap_dir / src.name
            shutil.copy2(src, dst)
            copied += 1
            self.append_log_line(f"✓ File trascinato in capitolo: {src.name} -> {dst.name}\n")

        self.show_toast(f"{copied} file aggiunti al capitolo")
        self.discover_current(preserve_selection=True)
        return True

    def on_add_subject_clicked(self) -> None:
        dlg = Adw.Dialog(title="Nuova materia", content_width=520, content_height=460)
        tv = Adw.ToolbarView()
        dlg.set_child(tv)

        hdr = Adw.HeaderBar()
        btn_save = Gtk.Button(label="Salva materia")
        btn_save.add_css_class("suggested-action")
        hdr.pack_end(btn_save)
        tv.add_top_bar(hdr)

        sw = Gtk.ScrolledWindow(vexpand=True)
        tv.set_content(sw)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(14)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        sw.set_child(box)

        grp = Adw.PreferencesGroup(
            title="Dettagli materia",
            description="Verrà aggiunta automaticamente al tuo config.toml",
        )
        box.append(grp)

        row_id = Adw.EntryRow(title="ID breve (es. fluidi, fisica)")
        row_name = Adw.EntryRow(title="Nome completo (es. Meccanica dei Fluidi)")
        row_src = Adw.EntryRow(title="Cartella sorgente PDF / .txt")
        row_proj = Adw.EntryRow(title="Cartella progetto locale LocalTypst")
        row_layout = Adw.ComboRow(
            title="Organizzazione file",
            model=Gtk.StringList.new(["Sottocartelle per capitolo (01 - Titolo)", "File piatti (modulo1.pdf)"]),
        )

        def pick_dir(target_row: Adw.EntryRow) -> None:
            fd = Gtk.FileDialog(title=f"Seleziona {target_row.get_title()}")

            def on_done(d: Gtk.FileDialog, res: Gio.AsyncResult) -> None:
                try:
                    gf = d.select_folder_finish(res)
                    if gf and gf.get_path():
                        target_row.set_text(gf.get_path())
                except Exception:
                    pass

            fd.select_folder(self, None, on_done)

        b_src = Gtk.Button(icon_name="folder-open-symbolic")
        b_src.set_valign(Gtk.Align.CENTER)
        b_src.add_css_class("flat")
        b_src.connect("clicked", lambda *_: pick_dir(row_src))
        row_src.add_suffix(b_src)

        b_proj = Gtk.Button(icon_name="folder-open-symbolic")
        b_proj.set_valign(Gtk.Align.CENTER)
        b_proj.add_css_class("flat")
        b_proj.connect("clicked", lambda *_: pick_dir(row_proj))
        row_proj.add_suffix(b_proj)

        def auto_fill_proj(*_: Any) -> None:
            k = re.sub(r"[^a-zA-Z0-9_]+", "_", row_id.get_text().strip()).strip("_")
            if k and (not row_proj.get_text().strip() or "LocalTypst/projects/Appunti_" in row_proj.get_text()):
                row_proj.set_text(str(Path.home() / f"Documents/LocalTypst/projects/Appunti_{k.upper()}_Locale"))

        row_id.connect("changed", auto_fill_proj)

        for r in (row_id, row_name, row_src, row_proj, row_layout):
            grp.add(r)

        def do_save(*_: Any) -> None:
            key = re.sub(r"[^a-z0-9_]+", "_", row_id.get_text().strip().lower()).strip("_")
            name = row_name.get_text().strip() or key.upper()
            src = row_src.get_text().strip()
            proj = row_proj.get_text().strip()
            if not key or not src or not proj:
                self.show_toast("Compila ID breve, cartella sorgente e cartella progetto")
                return
            if key in self.cfg.get("materie", {}):
                self.show_toast(f"La materia '{key}' esiste già in config.toml")
                return
            layout = "subfolders" if row_layout.get_selected() == 0 else "flat_files"
            try:
                append_subject_to_config(
                    self.config_path,
                    key=key,
                    name=name,
                    source_dir=src,
                    typst_project_dir=proj,
                    layout=layout,
                )
                self.config_path, self.cfg = load_config(self.config_path)
                self.materie = list(self.cfg.get("materie", {}).keys())
                mat_labels = [f"{k.upper()}" if len(k) <= 4 else k.capitalize() for k in self.materie]
                self._suppress_dropdown_cb = True
                self.materia_dd.set_model(Gtk.StringList.new(mat_labels))
                self.materia_dd.set_selected(self.materie.index(key))
                self._populate_chapters(prefer_cap=None)
                self._suppress_dropdown_cb = False
                self.discover_current(preserve_selection=False)
                dlg.close()
                self.show_toast(f"Materia '{name}' aggiunta!")
            except Exception as exc:
                self.show_toast(f"Errore salvataggio: {exc}")

        btn_save.connect("clicked", do_save)
        dlg.present(self)

    def get_selected_part_ids(self) -> list[str]:
        return [pid for pid, chk in self.part_checks.items() if chk.get_active()]

    def select_parts_preset(self, mode: str) -> None:
        m = self.current_manifest
        if not m:
            return
        by_id = {p["part_id"]: p for p in m["parts"]}
        for pid, chk in self.part_checks.items():
            part = by_id.get(pid)
            if not part or not part.get("pdf"):
                chk.set_active(False)
                continue
            if mode == "all":
                chk.set_active(True)
            elif mode == "none":
                chk.set_active(False)
            elif mode == "ready":
                ok = bool(part.get("txt")) or (
                    part["needs_transcription"] and self.sw_transcribe.get_active()
                )
                chk.set_active(ok)
        self.refresh_action_buttons()

    def refresh_action_buttons(self) -> None:
        running = self.proc is not None
        self.stop_btn.set_visible(running)
        self.stop_btn.set_sensitive(running)
        self.run_btn.set_visible(not running)
        self.refresh_btn.set_sensitive(not running)
        self.materia_dd.set_sensitive(not running)
        self.cap_dd.set_sensitive(not running)
        for chk in self.part_checks.values():
            if running:
                chk.set_sensitive(False)

        m = self.current_manifest
        if not m or m["missing"]["chapter_not_found"] or m["missing"]["no_pdfs"]:
            self.run_btn.set_sensitive(False)
            self.merge_btn.set_sensitive(False)
            self.parts_group.set_description("Nessuna parte selezionabile")
            return

        by_id = {p["part_id"]: p for p in m["parts"]}
        if not running:
            for pid, chk in self.part_checks.items():
                chk.set_sensitive(bool(by_id.get(pid, {}).get("pdf")))

        selected = self.get_selected_part_ids()
        total_pdf_parts = sum(1 for p in m["parts"] if p.get("pdf"))
        self.parts_group.set_description(
            f"{len(selected)} di {total_pdf_parts} parti selezionate per la generazione"
        )

        # Verifica validità SOLO sulle parti selezionate!
        skip_notes = self.sw_skip_notes.get_active()
        can_run = False
        block_reason = ""
        if not running and selected:
            if skip_notes:
                missing_gen = [pid for pid in selected if not self._part_generated_info(m, pid)[0]]
                if missing_gen:
                    block_reason = f"Con --skip-notes mancano i .typ generati per: {', '.join(missing_gen)}"
                else:
                    can_run = True
            else:
                blocked_no_txt = [
                    pid
                    for pid in selected
                    if not by_id[pid].get("txt")
                    and not (by_id[pid]["needs_transcription"] and self.sw_transcribe.get_active())
                ]
                if blocked_no_txt:
                    block_reason = f"Manca .txt per le parti selezionate: {', '.join(blocked_no_txt)}"
                else:
                    can_run = True
        elif not selected:
            block_reason = "Seleziona almeno una parte"

        self.run_btn.set_sensitive(can_run)
        if skip_notes:
            lbl = f"Rigenera Figure ({len(selected)})" if selected else "Rigenera Figure"
        elif len(selected) == total_pdf_parts and total_pdf_parts > 0:
            lbl = f"Genera Capitolo ({len(selected)})"
        elif len(selected) == 1:
            lbl = f"Genera Parte {selected[0]}"
        elif selected:
            lbl = f"Genera {len(selected)} Parti"
        else:
            lbl = "Genera"
        self.run_btn_content.set_label(lbl)
        self.run_btn.set_tooltip_text(block_reason if block_reason else "Avvia la pipeline sulle parti selezionate")

        if not self.tracker.active and self.tracker.finished_at is None:
            if block_reason and selected:
                self.activity_label.set_text(block_reason)
            elif selected:
                self.activity_label.set_text(f"Pronto — {len(selected)} parti selezionate")
            else:
                self.activity_label.set_text("Seleziona almeno una parte da generare")

        slug = m.get("chapter_slug", f"cap{m['capitolo']}")
        parts_dir = Path(m["typst_project_dir"]) / "_parts" / slug
        has_parts = parts_dir.exists() and any(parts_dir.glob("p*.typ"))
        self.merge_btn.set_sensitive((not running) and has_parts)

        # Aggiorna il banner automatico OCR sulle parti selezionate
        ocr_alerts: list[str] = []
        has_required = False
        for pid in selected:
            oi = (by_id.get(pid) or {}).get("ocr_info") or {}
            lvl = oi.get("level")
            if lvl in ("required", "recommended"):
                if lvl == "required":
                    has_required = True
                ocr_alerts.append(f"Parte {pid} ({oi.get('reason', oi.get('label', 'OCR'))})")

        force_on = self.sw_mineru.get_active() and self.sw_force_ocr.get_active()
        is_appunti_tab = self.view_stack.get_visible_child_name() == "appunti"
        if is_appunti_tab and ocr_alerts and not force_on:
            prefix = "OCR necessario" if has_required else "Forza OCR consigliato"
            detail = ocr_alerts[0] if len(ocr_alerts) == 1 else f"{len(ocr_alerts)} parti selezionate ({', '.join(p.split(' (')[0] for p in ocr_alerts)})"
            self.ocr_banner.set_title(f"{prefix}: {detail}")
            self.ocr_banner.set_revealed(True)
        else:
            self.ocr_banner.set_revealed(False)

    # --- Associa file .txt / Audio ---

    def on_attach_file(self, part: dict[str, Any]) -> None:
        dialog = Gtk.FileDialog(title=f"Scegli .txt o audio/video per Parte {part['part_id']}")
        f_supported = Gtk.FileFilter()
        f_supported.set_name("Trascrizioni e Audio/Video")
        for pat in ("*.txt", "*.mp4", "*.mkv", "*.webm", "*.mov", "*.mp3", "*.wav", "*.m4a", "*.ogg"):
            f_supported.add_pattern(pat)
        f_all = Gtk.FileFilter()
        f_all.set_name("Tutti i file")
        f_all.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f_supported)
        filters.append(f_all)
        dialog.set_filters(filters)
        dialog.open(self, None, lambda d, res: self._on_file_picked(d, res, part))

    def _on_file_picked(self, dialog: Gtk.FileDialog, res: Gio.AsyncResult, part: dict[str, Any]) -> None:
        try:
            gfile = dialog.open_finish(res)
        except Exception:
            return
        if not gfile or not self.current_manifest:
            return
        src = Path(gfile.get_path())
        chap_dir = Path(self.current_manifest["chapter_dir"])
        mat = self.current_manifest["materia"].upper()
        cap = self.current_manifest["capitolo"]
        pid = part["part_id"]
        ext = src.suffix.lower()
        dst = chap_dir / f"{mat}{cap:02d}_{pid}{ext}"
        shutil.copy2(src, dst)
        self.append_log_line(f"✓ File associato a Parte {pid}: {src.name} -> {dst}\n")
        self.show_toast(f"File associato a Parte {pid}")
        self.discover_current(preserve_selection=True)
        if pid in self.part_checks:
            self.part_checks[pid].set_active(True)
            self.refresh_action_buttons()

    # --- Pulizia capitolo generato ---

    def on_clean_chapter_clicked(self) -> None:
        if self.proc is not None:
            self.show_toast("Interrompi prima il processo in corso")
            return
        m = self.current_manifest
        if not m or m["missing"]["chapter_not_found"]:
            return
        slug = m["chapter_slug"]
        dlg = Adw.AlertDialog(
            heading=f"Pulire i file generati di '{slug}'?",
            body=(
                f"Verranno rimossi dalla copia locale:\n"
                f"• {slug}.typ e _parts/{slug}/\n"
                f"• le figure figs/{m['materia']}-c{m['capitolo']}-*\n\n"
                f"I PDF e i .txt sorgenti in {m['chapter_dir']} NON verranno toccati."
            ),
        )
        dlg.add_response("cancel", "Annulla")
        dlg.add_response("clean", "Elimina generati")
        dlg.set_response_appearance("clean", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", self._on_clean_confirmed)
        dlg.present(self)

    def _on_clean_confirmed(self, _dlg: Adw.AlertDialog, response: str) -> None:
        if response != "clean" or not self.current_manifest:
            return
        m = self.current_manifest
        typst_dir = Path(m["typst_project_dir"])
        slug = m["chapter_slug"]
        mat = m["materia"]
        cap = m["capitolo"]
        removed = 0

        cap_file = typst_dir / f"{slug}.typ"
        if cap_file.exists():
            cap_file.unlink()
            removed += 1
        parts_dir = typst_dir / "_parts" / slug
        if parts_dir.exists():
            shutil.rmtree(parts_dir)
            removed += 1
        figs_dir = typst_dir / m["figs_dir"]
        if figs_dir.exists():
            for f in figs_dir.glob(f"{mat}-c{cap}-*"):
                if f.is_file():
                    f.unlink()
                    removed += 1
            # Rigenera _index.typ pulito
            entries: dict[str, tuple[str, str]] = {}
            for p in sorted(figs_dir.iterdir()):
                if p.is_file() and not p.name.startswith("_"):
                    if p.suffix == ".typ":
                        entries[p.stem] = ("typ", p.name)
                    elif p.suffix == ".png" and p.stem not in entries:
                        entries[p.stem] = ("png", p.name)
            lines = ["// Registro automatico delle figure generate (non modificare a mano)"]
            if not entries:
                lines.append("#let fig-registry = (:)")
            else:
                lines.append("#let fig-registry = (")
                for fid, (ft, fn) in sorted(entries.items()):
                    lines.append(f'  "{fid}": (type: "{ft}", path: "{fn}"),')
                lines.append(")")
            (figs_dir / "_index.typ").write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Rimuovi anche eventuale #include "<slug>.typ" da main.typ
        main_path = typst_dir / m["main_file"]
        inc = f'#include "{slug}.typ"'
        if main_path.exists():
            txt = main_path.read_text(encoding="utf-8")
            new_txt = "\n".join(ln for ln in txt.splitlines() if ln.strip() != inc) + "\n"
            if new_txt != txt:
                main_path.write_text(new_txt, encoding="utf-8")

        self.append_log_line(f"✓ Pulizia completata per '{slug}' ({removed} elementi rimossi).\n")
        self.show_toast(f"File generati di '{slug}' rimossi")
        self.discover_current(preserve_selection=True)

    # --- Logging & Rendering Progress/ETA ---

    def append_log_line(self, line: str) -> None:
        # Intercetta eventi strutturati @@PROGRESS senza sporcare il log visibile
        stripped = line.strip()
        if stripped.startswith(PROGRESS_PREFIX):
            raw = stripped[len(PROGRESS_PREFIX) :].strip()
            try:
                ev = json.loads(raw)
                self.tracker.handle_event(ev)
                self._render_progress_ui()
            except Exception:
                pass
            return

        end = self.log_buf.get_end_iter()
        tag_name: str | None = None
        if stripped.startswith("==>"):
            tag_name = "phase"
        elif stripped.startswith("$ "):
            tag_name = "cmd"
        elif any(k in stripped for k in ("ERRORE", "ERROR", "fallit", "Traceback")):
            tag_name = "err"
        elif any(k in stripped for k in ("Fallback", "Retry", "Avviso", "STOP")):
            tag_name = "warn"
        elif stripped.startswith("✓") or "Completato" in stripped or "compilato con successo" in stripped:
            tag_name = "ok"

        if tag_name:
            self.log_buf.insert_with_tags_by_name(end, line, tag_name)
        else:
            self.log_buf.insert(end, line)

        self._log_line_count += line.count("\n") or 1
        self.log_count_chip.set_label(f"{self._log_line_count} righe")

        mark = self.log_buf.create_mark(None, self.log_buf.get_end_iter(), False)
        self.log_view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)

    def _render_progress_ui(self) -> None:
        frac, elapsed, eta = self.tracker.snapshot()
        self.progress_bar.set_fraction(frac)
        self.percent_label.set_text(f"{int(round(frac * 100))}%")

        if self.tracker.active:
            self.spinner.set_visible(True)
            self.spinner.start()
            self.activity_label.set_text(self.tracker.current_message)
            self.elapsed_label.set_text(f"Trascorso: {fmt_duration(elapsed)}")
            self.eta_label.set_text(f"Stimato: {fmt_eta(eta)}")
        else:
            self.spinner.stop()
            self.spinner.set_visible(False)
            if self.tracker.finished_at is not None:
                self.activity_label.set_text(self.tracker.current_message)
                self.elapsed_label.set_text(f"Durata: {fmt_duration(elapsed)}")
                self.eta_label.set_text(
                    "Completato ✓" if self.tracker.current_message.startswith("Completato") else "Interrotto"
                )
            else:
                self.elapsed_label.set_text("Trascorso: —")
                self.eta_label.set_text("Stimato: —")

        # Contatori parti & figure
        parts_str = (
            f"{self.tracker.notes_done}/{self.tracker.notes_total}"
            if self.tracker.notes_total > 0
            else "—"
        )
        figs_str = (
            f"{self.tracker.figs_done}/{self.tracker.figs_total}"
            if self.tracker.figs_total > 0
            else "—"
        )
        self.counts_label.set_text(f"Parti: {parts_str}  ·  Figure: {figs_str}")

        # Aggiorna pills dello stepper fasi
        base_texts = {
            "discovery": "1. Discovery",
            "mineru": "2. MinerU OCR",
            "transcribe": "3. Trascrivi",
            "notes": "4. Appunti",
            "figures": "5. Figure CeTZ",
            "insert": "6. Compila",
            "check": "7. Check",
        }
        for key, lbl in self.stage_labels.items():
            for cls in ("stage-active", "stage-done", "stage-skip"):
                lbl.remove_css_class(cls)
            st = self.tracker.phases.get(key, PhasePlan(key)).status
            if st == "active":
                lbl.add_css_class("stage-active")
                lbl.set_text(base_texts[key])
            elif st == "done":
                lbl.add_css_class("stage-done")
                lbl.set_text(f"✓ {base_texts[key].split('. ', 1)[1]}")
            elif st == "skip":
                lbl.add_css_class("stage-skip")
                lbl.set_text(base_texts[key])
            else:
                lbl.set_text(base_texts[key])

    def _on_timer_tick(self) -> bool:
        if not self.tracker.active:
            self._tick_source_id = None
            return False
        self._render_progress_ui()
        return True

    # --- Esecuzione & Interruzione pulita ---

    def run_bg(self, cmd: list[str]) -> None:
        self.append_log_line(f"\n$ {' '.join(cmd)}\n")
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=env,
        )
        self.refresh_action_buttons()
        self._render_progress_ui()

        if self._tick_source_id is None:
            self._tick_source_id = GLib.timeout_add_seconds(1, self._on_timer_tick)

        def worker() -> None:
            assert self.proc and self.proc.stdout
            for line in self.proc.stdout:
                GLib.idle_add(self.append_log_line, line)
            rc = self.proc.wait()
            GLib.idle_add(self._on_proc_done, rc)

        threading.Thread(target=worker, daemon=True).start()

    def _on_proc_done(self, rc: int) -> None:
        self.proc = None
        ok = rc == 0
        self.tracker.finish(ok)
        self._render_progress_ui()
        self.append_log_line(f"\n[Terminato con codice {rc}]\n")

        slug = self.current_manifest.get("chapter_slug", "") if self.current_manifest else ""
        if ok:
            self.show_toast(f"Completato: {slug}")
            send_notify("Appunti completati!", f"Capitolo '{slug}' pronto per la revisione.")
        elif rc in (-signal.SIGTERM, -signal.SIGKILL, 143, 137):
            self.show_toast("Esecuzione interrotta")
        else:
            self.show_toast(f"Processo terminato con errore (codice {rc})")
            send_notify(
                "Appunti: intervento richiesto",
                f"Processo fermato con codice {rc}. Controlla la finestra Appunti.",
                urgency="critical",
            )
        self.discover_current(preserve_selection=True)

    def _terminate_proc_group(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

        def ensure_dead() -> None:
            time.sleep(2.0)
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        threading.Thread(target=ensure_dead, daemon=True).start()

    def on_run_clicked(self, *_: Any) -> None:
        mat = self.current_materia()
        cap = self.current_chapter_num()
        m = self.current_manifest
        if cap is None or not m:
            return

        selected = self.get_selected_part_ids()
        if not selected:
            self.show_toast("Seleziona almeno una parte")
            return

        total_pdf_parts = [p["part_id"] for p in m["parts"] if p.get("pdf")]
        by_id = {p["part_id"]: p for p in m["parts"]}

        cmd = [
            str(APPUNTI_BIN),
            mat,
            str(cap),
            "--config",
            str(self.config_path),
            "--no-gui",
            f"--figures-mode={self.current_figures_mode()}",
            f"--backend={self.current_backend()}",
        ]
        if len(selected) < len(total_pdf_parts):
            cmd.append(f"--part={','.join(selected)}")
        cmd.append("--mineru" if self.sw_mineru.get_active() else "--no-mineru")
        if self.sw_mineru.get_active() and self.sw_force_ocr.get_active():
            cmd.append("--force-ocr")
        if not self.sw_visual.get_active():
            cmd.append("--no-check")
        if self.sw_transcribe.get_active():
            cmd.append("--transcribe")
        if self.sw_force_figs.get_active():
            cmd.append("--force-figures")
        if self.sw_skip_notes.get_active():
            cmd.append("--skip-notes")
        if self.sw_merge.get_active():
            cmd.append("--merge")

        will_tx = self.sw_transcribe.get_active() and any(
            by_id[pid]["needs_transcription"] for pid in selected
        )
        known_figs: int | None = None
        if self.sw_skip_notes.get_active():
            known_figs = sum(self._part_generated_info(m, pid)[2] for pid in selected)

        self.tracker.start_run(
            n_parts=len(selected),
            will_transcribe=will_tx,
            use_mineru=self.sw_mineru.get_active(),
            skip_notes=self.sw_skip_notes.get_active(),
            will_check=self.sw_visual.get_active(),
            will_merge=self.sw_merge.get_active(),
            known_fig_count=known_figs,
        )
        self.run_bg(cmd)

    def on_merge_clicked(self, *_: Any) -> None:
        mat = self.current_materia()
        cap = self.current_chapter_num()
        if cap is None:
            return
        self.tracker.start_run(
            n_parts=0,
            will_transcribe=False,
            skip_notes=True,
            will_check=False,
            will_merge=True,
            merge_only=True,
        )
        self.run_bg([str(APPUNTI_BIN), "merge", mat, str(cap), "--config", str(self.config_path)])

    def on_stop_clicked(self, *_: Any) -> None:
        if self.proc:
            self.append_log_line("\n[Interruzione richiesta — chiusura di tutti i sottoprocessi...]\n")
            self._terminate_proc_group()

    def on_close_request(self, *_: Any) -> bool:
        if self.proc is not None:
            self._terminate_proc_group()
        return False


class AppuntiApp(Adw.Application):
    def __init__(
        self,
        init_materia: str | None = None,
        init_cap: int | None = None,
        auto_run: bool = False,
        extra_flags: list[str] | None = None,
        init_tab: str = "appunti",
        config_path: Path | None = None,
    ) -> None:
        super().__init__(application_id="local.appunti.typst.studio", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.init_materia = init_materia
        self.init_cap = init_cap
        self.auto_run = auto_run
        self.extra_flags = extra_flags or []
        self.init_tab = init_tab
        self.config_path = config_path
        self._win: AppuntiWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.DEFAULT)
        provider = Gtk.CssProvider()
        provider.load_from_string(CUSTOM_CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
            icon_theme = Gtk.IconTheme.get_for_display(display)
            if ASSETS_DIR.exists():
                icon_theme.add_search_path(str(ASSETS_DIR))

    def do_activate(self) -> None:
        if self._win is None:
            self._win = AppuntiWindow(
                self,
                init_materia=self.init_materia,
                init_cap=self.init_cap,
                auto_run=self.auto_run,
                extra_flags=self.extra_flags,
                init_tab=self.init_tab,
                config_path=self.config_path,
            )
        self._win.present()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--materia", default=None)
    parser.add_argument("--cap", type=int, default=None)
    parser.add_argument("--tab", default="appunti", choices=("appunti", "cetz"))
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--auto-run", action="store_true")
    known, remaining = parser.parse_known_args(sys.argv[1:])
    extra = [x for x in remaining if x.startswith("--")]

    app = AppuntiApp(
        init_materia=known.materia,
        init_cap=known.cap,
        auto_run=known.auto_run,
        extra_flags=extra,
        init_tab=known.tab,
        config_path=known.config,
    )
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
