"""Vista Libadwaita / GTK 4 unificata per CeTZ Studio (scheda integrata in Appunti Typst Studio).

Caratteristiche:
- Coda immagini + convertitore batch di file .typ (Revisione guidata o Automatica)
- Confronto fianco a fianco Originale vs Render CeTZ
- Codice CeTZ modificabile live con Rerun + raffinamento iterativo AI
- Usa AgyStreamSession a blocchi (niente picchi RAM da 5 processi paralleli)
- Barra dei progressi reale + stima ETA + interruzione pulita + clipboard nativa GDK
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from .agy_client import create_llm_session
from .cetz_engine import (
    apply_typ_replacements,
    convert_single_image,
    convert_typ_batches,
    parse_typ_images,
    refine_cetz_code,
    render_cetz_png,
)
from .figures import update_figs_index
from .models import AGY_GEMINI_MODELS, AGY_MODEL_IDS, GEMINI_API_MODEL_IDS, GEMINI_API_MODELS


def _fmt_sec(sec: float | None) -> str:
    if sec is None or sec < 0:
        return "—"
    s = int(round(sec))
    m, s = divmod(s, 60)
    return f"{m:02d}:{s:02d}"


def _fmt_eta_short(sec: float | None) -> str:
    if sec is None or sec < 0:
        return "Calcolo stima…"
    s = int(round(sec))
    if s < 5:
        return "Quasi pronto…"
    m, s = divmod(s, 60)
    return f"~{m}m {s:02d}s" if m > 0 else f"~{s}s"


class CeTZStudioView(Gtk.Box):
    """Scheda CeTZ Studio completamente integrata nel design Libadwaita."""

    def __init__(
        self,
        *,
        window: Adw.ApplicationWindow,
        general_cfg: dict[str, Any],
        toast_cb: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.set_vexpand(True)
        self.set_hexpand(True)

        self.win = window
        self.gen = general_cfg
        self.toast_cb = toast_cb

        self.agy_bin = general_cfg["agy_bin"]
        self.typst_bin = general_cfg["typst_bin"]
        self.backup_dir = Path(general_cfg["cetz_backup_dir"]).expanduser()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir = Path(general_cfg["screenshots_dir"]).expanduser()
        self.prompt_file = Path(general_cfg["prompts_dir"]) / "figure.md"
        self.batch_size = int(general_cfg.get("figure_batch_size", 5))
        self.max_retries = int(general_cfg.get("figure_max_retries", 2))

        self.items: list[dict[str, Any]] = []
        self.typ_state: dict[str, Any] = {}
        self.selected_idx: int = -1
        self._guard_ui = False
        self._busy = False
        self._cancel_ev = threading.Event()

        # Tracciamento progressi + ETA
        self._run_started_at: float | None = None
        self._run_done = 0
        self._run_total = 0
        self._run_durations: list[float] = []
        self._run_label = "Pronto"
        self._tick_id: int | None = None

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(310)
        paned.set_vexpand(True)
        paned.set_hexpand(True)
        self.append(paned)

        paned.set_start_child(self._build_sidebar())
        paned.set_end_child(self._build_workspace())
        paned.set_resize_start_child(False)
        paned.set_shrink_start_child(False)

        # Drag & Drop di immagini o file .typ direttamente sulla scheda CeTZ Studio
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self.on_files_dropped)
        self.add_controller(drop)

    def _build_sidebar(self) -> Gtk.Widget:
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        side.set_margin_top(14)
        side.set_margin_bottom(14)
        side.set_margin_start(14)
        side.set_margin_end(10)
        side.set_size_request(295, -1)

        # Card controlli sorgente, backend e modello
        ctrl_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        ctrl_card.add_css_class("card")
        ctrl_card.add_css_class("cetz-side-card")
        side.append(ctrl_card)

        lbl_b = Gtk.Label(label="Backend & Modello Gemini", xalign=0)
        lbl_b.add_css_class("caption-heading")
        lbl_b.add_css_class("dim-label")
        ctrl_card.append(lbl_b)

        self.backend_dd = Gtk.DropDown.new_from_strings(["Antigravity CLI (agy)", "Gemini REST API (API key)"])
        if self.gen.get("llm_backend") == "gemini-api":
            self.backend_dd.set_selected(1)
        self.backend_dd.connect("notify::selected", self.on_backend_changed)
        ctrl_card.append(self.backend_dd)

        self._active_model_ids: list[str] = []
        self.model_dd = Gtk.DropDown()
        self._populate_models_for_backend()
        ctrl_card.append(self.model_dd)

        # Azioni aggiunta immagini / conversione .typ
        self.btn_add_imgs = Gtk.Button()
        self.btn_add_imgs.add_css_class("suggested-action")
        self.btn_add_imgs.add_css_class("pill")
        self.btn_add_imgs.set_child(
            Adw.ButtonContent(icon_name="list-add-symbolic", label="Aggiungi immagini…")
        )
        self.btn_add_imgs.connect("clicked", self.on_pick_images)
        ctrl_card.append(self.btn_add_imgs)

        typ_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ctrl_card.append(typ_row)

        self.btn_typ = Gtk.Button()
        self.btn_typ.set_hexpand(True)
        self.btn_typ.set_child(
            Adw.ButtonContent(icon_name="document-open-symbolic", label="Converti .typ…")
        )
        self.btn_typ.connect("clicked", self.on_pick_typ)
        typ_row.append(self.btn_typ)

        self.mode_dd = Gtk.DropDown.new_from_strings(["Revisione", "Automatica"])
        self.mode_dd.set_tooltip_text(
            "Revisione (Non Fidata): controlla ogni figura prima di salvare\n"
            "Automatica (Fidata): sostituisce subito nel .typ con backup .bak"
        )
        typ_row.append(self.mode_dd)

        self.chk_skip_non_study = Gtk.CheckButton(label="Salta foto/immagini non tecniche (SKIP)")
        self.chk_skip_non_study.set_active(False)
        ctrl_card.append(self.chk_skip_non_study)

        # Intestazione coda
        q_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        q_lbl = Gtk.Label(label="Coda figure", xalign=0)
        q_lbl.add_css_class("heading")
        q_lbl.set_hexpand(True)
        q_hdr.append(q_lbl)
        self.queue_count_chip = Gtk.Label(label="0")
        self.queue_count_chip.add_css_class("meta-chip")
        q_hdr.append(self.queue_count_chip)
        side.append(q_hdr)

        # Lista coda
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.queue_list = Gtk.ListBox()
        self.queue_list.add_css_class("boxed-list")
        self.queue_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.queue_list.connect("row-selected", self.on_queue_row_selected)
        sw.set_child(self.queue_list)
        side.append(sw)

        # Pulsanti coda in fondo
        q_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        side.append(q_actions)

        self.btn_run_queue = Gtk.Button(label="Elabora coda")
        self.btn_run_queue.set_hexpand(True)
        self.btn_run_queue.connect("clicked", self.on_process_queue)
        q_actions.append(self.btn_run_queue)

        self.btn_stop = Gtk.Button(icon_name="process-stop-symbolic")
        self.btn_stop.add_css_class("destructive-action")
        self.btn_stop.set_tooltip_text("Interrompi elaborazione")
        self.btn_stop.set_visible(False)
        self.btn_stop.connect("clicked", self.on_cancel_clicked)
        q_actions.append(self.btn_stop)

        self.btn_clear = Gtk.Button(icon_name="edit-clear-symbolic")
        self.btn_clear.set_tooltip_text("Svuota coda")
        self.btn_clear.connect("clicked", lambda *_: self.clear_all())
        q_actions.append(self.btn_clear)

        return side

    def _build_workspace(self) -> Gtk.Widget:
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        right.set_margin_top(14)
        right.set_margin_bottom(14)
        right.set_margin_start(8)
        right.set_margin_end(14)
        right.set_hexpand(True)
        right.set_vexpand(True)

        # Barra avanzamento + ETA (visibile durante elaborazioni batch/coda)
        self.prog_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.prog_card.add_css_class("card")
        self.prog_card.add_css_class("cetz-side-card")
        self.prog_card.set_visible(False)
        right.append(self.prog_card)

        p_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.prog_spinner = Gtk.Spinner()
        p_top.append(self.prog_spinner)
        self.prog_status = Gtk.Label(label="Elaborazione…", xalign=0, hexpand=True)
        self.prog_status.set_ellipsize(Pango.EllipsizeMode.END)
        p_top.append(self.prog_status)
        self.prog_eta = Gtk.Label(label="Stimato: —")
        self.prog_eta.add_css_class("stat-pill")
        p_top.append(self.prog_eta)
        self.prog_card.append(p_top)

        self.prog_bar = Gtk.ProgressBar()
        self.prog_bar.add_css_class("appunti-progress")
        self.prog_card.append(self.prog_bar)

        # Barra revisione .typ (visibile solo in modalità revisione .typ)
        self.typ_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.typ_bar.add_css_class("card")
        self.typ_bar.add_css_class("cetz-side-card")
        self.typ_bar.set_visible(False)
        right.append(self.typ_bar)

        b_prev = Gtk.Button(icon_name="go-previous-symbolic")
        b_prev.set_tooltip_text("Figura precedente")
        b_prev.connect("clicked", lambda *_: self.select_index(self.selected_idx - 1))
        b_next = Gtk.Button(icon_name="go-next-symbolic")
        b_next.set_tooltip_text("Figura successiva")
        b_next.connect("clicked", lambda *_: self.select_index(self.selected_idx + 1))

        nav_linked = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        nav_linked.add_css_class("linked")
        nav_linked.append(b_prev)
        nav_linked.append(b_next)
        self.typ_bar.append(nav_linked)

        self.lbl_typ_review = Gtk.Label(xalign=0, hexpand=True)
        self.lbl_typ_review.set_ellipsize(Pango.EllipsizeMode.END)
        self.typ_bar.append(self.lbl_typ_review)

        self.btn_keep_orig = Gtk.ToggleButton(label="Lascia originale")
        self.btn_keep_orig.connect("toggled", self.on_keep_orig_toggled)
        self.typ_bar.append(self.btn_keep_orig)

        self.btn_save_typ = Gtk.Button(label="Salva .typ (.bak)")
        self.btn_save_typ.add_css_class("suggested-action")
        self.btn_save_typ.add_css_class("pill")
        self.btn_save_typ.connect("clicked", self.on_save_typ)
        self.typ_bar.append(self.btn_save_typ)

        # Barra modifica figura del progetto (ponte diretto dalla scheda Appunti)
        self.proj_fig_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.proj_fig_bar.add_css_class("card")
        self.proj_fig_bar.add_css_class("cetz-side-card")
        self.proj_fig_bar.set_visible(False)
        right.append(self.proj_fig_bar)

        self.lbl_proj_fig = Gtk.Label(xalign=0, hexpand=True)
        self.lbl_proj_fig.set_ellipsize(Pango.EllipsizeMode.END)
        self.proj_fig_bar.append(self.lbl_proj_fig)

        self.btn_save_proj_fig = Gtk.Button(label="Salva nel progetto (Live LocalTypst)")
        self.btn_save_proj_fig.add_css_class("suggested-action")
        self.btn_save_proj_fig.add_css_class("pill")
        self.btn_save_proj_fig.connect("clicked", self.on_save_project_figure)
        self.proj_fig_bar.append(self.btn_save_proj_fig)

        # Confronto fianco a fianco Originale vs CeTZ
        cmp_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        cmp_box.set_vexpand(True)
        right.append(cmp_box)

        self.orig_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.orig_card.add_css_class("card")
        self.orig_card.add_css_class("cetz-preview-card")
        lbl_o = Gtk.Label(label="Originale", xalign=0)
        lbl_o.add_css_class("heading")
        self.orig_card.append(lbl_o)
        self.pic_orig = Gtk.Picture(can_shrink=True, vexpand=True, hexpand=True)
        self.orig_card.append(self.pic_orig)
        cmp_box.append(self.orig_card)

        cetz_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        cetz_card.add_css_class("card")
        cetz_card.add_css_class("cetz-preview-card")
        lbl_c = Gtk.Label(label="Render CeTZ", xalign=0)
        lbl_c.add_css_class("heading")
        cetz_card.append(lbl_c)
        self.pic_cetz = Gtk.Picture(can_shrink=True, vexpand=True, hexpand=True)
        cetz_card.append(self.pic_cetz)
        cmp_box.append(cetz_card)

        # Editor codice CeTZ espandibile
        self.code_tv = Gtk.TextView(editable=True, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.code_tv.add_css_class("log-view")
        self.code_buf = self.code_tv.get_buffer()
        self.code_buf.connect("changed", self.on_code_changed)
        code_sw = Gtk.ScrolledWindow(min_content_height=165, child=self.code_tv)
        self.code_exp = Gtk.Expander(label="Codice CeTZ (modificabile)", child=code_sw)
        right.append(self.code_exp)

        # Stato elemento selezionato
        self.status_lbl = Gtk.Label(
            label="Aggiungi immagini o carica un file .typ per iniziare la conversione in CeTZ.",
            xalign=0,
            wrap=True,
        )
        self.status_lbl.add_css_class("caption")
        self.status_lbl.add_css_class("dim-label")
        right.append(self.status_lbl)

        # Barra modifica AI + Rerun + Copia
        ai_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        right.append(ai_bar)

        self.ent_ai = Gtk.Entry(
            placeholder_text="Chiedi modifica AI (es. sposta etichetta, aggiungi freccia, cambia scala)…",
            hexpand=True,
        )
        self.ent_ai.connect("activate", self.on_ai_edit)
        ai_bar.append(self.ent_ai)

        self.btn_ai = Gtk.Button(label="Modifica AI")
        self.btn_ai.add_css_class("suggested-action")
        self.btn_ai.connect("clicked", self.on_ai_edit)
        ai_bar.append(self.btn_ai)

        self.btn_rerun = Gtk.Button(label="Rerun")
        self.btn_rerun.set_tooltip_text("Ricompila il codice CeTZ modificato a mano")
        self.btn_rerun.connect("clicked", self.on_rerun_clicked)
        ai_bar.append(self.btn_rerun)

        self.btn_copy = Gtk.Button()
        self.btn_copy.set_child(Adw.ButtonContent(icon_name="edit-copy-symbolic", label="Copia"))
        self.btn_copy.connect("clicked", self.on_copy_code)
        ai_bar.append(self.btn_copy)

        return right

    # --- Helpers ---

    def current_backend(self) -> str:
        return "gemini-api" if self.backend_dd.get_selected() == 1 else "agy"

    def _populate_models_for_backend(self) -> None:
        if self.current_backend() == "gemini-api":
            pairs = GEMINI_API_MODELS
            default_m = self.gen.get("gemini_api_model", "gemini-2.5-pro")
        else:
            pairs = AGY_GEMINI_MODELS
            default_m = self.gen.get("model_cetz_studio", "gemini-4-hydrogen-high")
        self._active_model_ids = [p[0] for p in pairs]
        self.model_dd.set_model(Gtk.StringList.new([p[1] for p in pairs]))
        if default_m in self._active_model_ids:
            self.model_dd.set_selected(self._active_model_ids.index(default_m))
        else:
            self.model_dd.set_selected(0)

    def on_backend_changed(self, *_: Any) -> None:
        self._populate_models_for_backend()

    def current_model(self) -> str:
        idx = self.model_dd.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self._active_model_ids):
            return self._active_model_ids[0]
        return self._active_model_ids[idx]

    def on_files_dropped(self, _target: Gtk.DropTarget, value: Any, _x: float, _y: float) -> bool:
        if self._busy or not isinstance(value, Gdk.FileList):
            return False
        files = value.get_files()
        paths = [Path(f.get_path()) for f in files if f.get_path()]
        if not paths:
            return False
        typ_files = [p for p in paths if p.suffix.lower() == ".typ"]
        img_files = [p for p in paths if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
        if typ_files:
            self._start_typ_conversion(typ_files[0])
            return True
        if img_files:
            self._enqueue_images(img_files)
            return True
        self.toast_cb("Trascina immagini (PNG/JPG/WebP) oppure un file .typ")
        return False

    def open_project_figure(
        self,
        *,
        fig_id: str,
        fig_path: Path,
        figs_dir: Path,
        orig_img: Path | None = None,
        on_saved: Callable[[], None] | None = None,
    ) -> None:
        """Apre una figura già generata dalla pipeline direttamente in CeTZ Studio per rifinirla live."""
        if self._busy:
            self.toast_cb("Attendi il termine dell'elaborazione corrente in CeTZ Studio")
            return
        if self.typ_state:
            self.clear_all()

        code = ""
        is_typ = fig_path.suffix.lower() == ".typ" and fig_path.exists()
        if is_typ:
            code = fig_path.read_text(encoding="utf-8").strip()

        display_orig = orig_img if (orig_img and orig_img.exists()) else (fig_path if not is_typ else None)
        stem = f"proj_{fig_id}_{int(time.time())}"
        png_out = ""
        status = "Pronto" if is_typ else "Ritaglio (convertibile)"
        ok = False
        err = ""
        if is_typ and code:
            ok, pp, comp_err = render_cetz_png(self.typst_bin, code, self.backup_dir, stem)
            png_out = str(pp) if ok else ""
            if not ok:
                status = "Errore Typst"
                err = comp_err

        it = {
            "path": display_orig,
            "name": f"{fig_id} (progetto)",
            "stem": stem,
            "st": status,
            "code": code,
            "png": png_out,
            "ok": ok,
            "err": err,
            "keep_orig": False,
            "dirty": False,
            "project_target": {
                "fig_id": fig_id,
                "figs_dir": figs_dir,
                "on_saved": on_saved,
            },
        }
        self.items.append(it)
        self._append_queue_row(it)
        self.code_exp.set_expanded(True)
        self.select_index(len(self.items) - 1)
        if not is_typ:
            self.toast_cb(f"Aperto ritaglio '{fig_id}': scrivi un'istruzione o premi Modifica AI per vettorializzarlo in CeTZ")
        else:
            self.toast_cb(f"Figura '{fig_id}' caricata in CeTZ Studio")

    def on_save_project_figure(self, *_: Any) -> None:
        if self.selected_idx < 0 or self.selected_idx >= len(self.items):
            return
        it = self.items[self.selected_idx]
        target = it.get("project_target")
        if not target:
            return
        code = self.code_buf.get_text(self.code_buf.get_start_iter(), self.code_buf.get_end_iter(), True).strip()
        if not code:
            self.toast_cb("Nessun codice CeTZ da salvare")
            return
        if "@preview/cetz:" not in code and "_macros.typ" not in code and "cetz-macros.typ" not in code:
            code = '#import "@preview/cetz:0.3.4": canvas, draw\n' + code
        ok, pp, err = render_cetz_png(self.typst_bin, code, self.backup_dir, it["stem"])
        if not ok:
            self._update_item(self.selected_idx, {"ok": False, "status": "Errore Typst", "code": code, "png": "", "err": err})
            self.toast_cb("Impossibile salvare: errore di compilazione Typst")
            return

        figs_dir: Path = target["figs_dir"]
        fig_id: str = target["fig_id"]
        out_typ = figs_dir / f"{fig_id}.typ"
        tmp_typ = figs_dir / f"{fig_id}.typ.tmp"
        tmp_typ.write_text(code + "\n", encoding="utf-8")
        tmp_typ.replace(out_typ)
        for ext in (".png", ".jpg", ".jpeg"):
            old_img = figs_dir / f"{fig_id}{ext}"
            if old_img.exists():
                old_img.unlink()
        update_figs_index(figs_dir)
        self._update_item(self.selected_idx, {"ok": True, "status": "Pronto", "code": code, "png": str(pp), "err": ""})
        self.toast_cb(f"Salvato {out_typ.name} — LocalTypst aggiornato live!")
        if target.get("on_saved"):
            target["on_saved"]()

    def _set_busy(self, busy: bool, total: int = 0, label: str = "") -> None:
        self._busy = busy
        self.btn_stop.set_visible(busy)
        self.btn_run_queue.set_sensitive(not busy)
        self.btn_add_imgs.set_sensitive(not busy)
        self.btn_typ.set_sensitive(not busy)
        self.btn_ai.set_sensitive(not busy)
        self.btn_clear.set_sensitive(not busy)

        if busy:
            self._cancel_ev.clear()
            self._run_started_at = time.monotonic()
            self._run_done = 0
            self._run_total = max(1, total)
            self._run_durations = []
            self._run_label = label or "Elaborazione CeTZ…"
            self.prog_card.set_visible(True)
            self.prog_spinner.start()
            self._render_progress()
            if self._tick_id is None:
                self._tick_id = GLib.timeout_add_seconds(1, self._on_tick)
        else:
            self.prog_spinner.stop()
            if self._tick_id is not None:
                GLib.source_remove(self._tick_id)
                self._tick_id = None
            self.prog_bar.set_fraction(1.0 if self._run_done >= self._run_total and self._run_total > 0 else 0.0)
            if self._run_started_at is not None:
                elapsed = time.monotonic() - self._run_started_at
                self.prog_eta.set_text(f"Durata: {_fmt_sec(elapsed)}")
                self.prog_status.set_text(label or "Completato")

    def _render_progress(self) -> None:
        if not self._busy or self._run_started_at is None:
            return
        now = time.monotonic()
        elapsed = now - self._run_started_at
        avg = (sum(self._run_durations[-5:]) / len(self._run_durations[-5:])) if self._run_durations else 24.0
        rem_items = max(0, self._run_total - self._run_done)
        frac = min(0.98, max(0.03, self._run_done / max(1, self._run_total)))
        eta = rem_items * avg if rem_items > 0 else 1.0
        self.prog_bar.set_fraction(frac)
        self.prog_status.set_text(f"{self._run_label} ({self._run_done}/{self._run_total})")
        self.prog_eta.set_text(f"Trascorso: {_fmt_sec(elapsed)}  ·  Stimato: {_fmt_eta_short(eta)}")

    def _on_tick(self) -> bool:
        if not self._busy:
            self._tick_id = None
            return False
        self._render_progress()
        return True

    def _badge_for_status(self, st: str, keep_orig: bool) -> tuple[str, str]:
        if keep_orig:
            return "Originale", "badge-warn"
        if st == "Pronto":
            return "Pronto", "badge-ready"
        if st in ("Errore Typst", "Errore agy"):
            return st, "badge-err"
        if st == "Ignorata":
            return "SKIP", "badge-warn"
        return st, "badge-generated"

    def _refresh_row_widget(self, it: dict[str, Any]) -> None:
        badge_txt, badge_cls = self._badge_for_status(it["st"], bool(it.get("keep_orig")))
        b: Gtk.Label = it["badge"]
        b.set_text(badge_txt)
        for c in ("badge-ready", "badge-generated", "badge-warn", "badge-err"):
            b.remove_css_class(c)
        b.add_css_class(badge_cls)

    def _append_queue_row(self, it: dict[str, Any]) -> None:
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row_box.set_margin_top(7)
        row_box.set_margin_bottom(7)
        row_box.set_margin_start(10)
        row_box.set_margin_end(10)

        lbl = Gtk.Label(label=it["name"], xalign=0, hexpand=True)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        row_box.append(lbl)

        badge = Gtk.Label()
        badge.add_css_class("status-badge")
        row_box.append(badge)
        it["badge"] = badge
        self._refresh_row_widget(it)

        lr = Gtk.ListBoxRow(child=row_box)
        self.queue_list.append(lr)
        self.queue_count_chip.set_label(str(len(self.items)))

    def select_index(self, idx: int) -> None:
        if 0 <= idx < len(self.items):
            row = self.queue_list.get_row_at_index(idx)
            if row is not None:
                self.queue_list.select_row(row)

    def on_queue_row_selected(self, _lb: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is not None:
            self.show_item(row.get_index())

    def show_item(self, idx: int) -> None:
        if idx < 0 or idx >= len(self.items):
            return
        self.selected_idx = idx
        it = self.items[idx]
        self._guard_ui = True
        try:
            self.code_buf.set_text(it["code"])
            self.btn_keep_orig.set_active(bool(it.get("keep_orig")))
        finally:
            self._guard_ui = False

        st_txt = "Mantiene immagine originale" if it.get("keep_orig") else it["st"]
        if it.get("err") and not it.get("keep_orig"):
            st_txt = f"{st_txt}: {it['err'].splitlines()[0][:120]}"
        self.status_lbl.set_text(f"{it['name']} — {st_txt}")

        has_orig = bool(it.get("path") and Path(it["path"]).exists())
        self.orig_card.set_visible(has_orig)
        self.pic_orig.set_filename(str(it["path"]) if has_orig else None)
        self.pic_cetz.set_filename(str(it["png"]) if it.get("png") and Path(it["png"]).exists() else None)

        if self.typ_state:
            self.lbl_typ_review.set_text(
                f"Revisione: {Path(self.typ_state['path']).name}  ({idx + 1}/{len(self.items)})"
            )
        target = it.get("project_target")
        if target:
            self.proj_fig_bar.set_visible(True)
            self.lbl_proj_fig.set_text(f"Figura progetto: {target['fig_id']}.typ ({target['figs_dir'].parent.name})")
        else:
            self.proj_fig_bar.set_visible(False)

    def _update_item(self, idx: int, res: dict[str, Any]) -> None:
        if idx < 0 or idx >= len(self.items):
            return
        it = self.items[idx]
        it["st"] = res.get("status", it["st"])
        if "code" in res:
            it["code"] = res["code"]
        if "png" in res:
            it["png"] = res["png"]
        if "ok" in res:
            it["ok"] = res["ok"]
            it["dirty"] = False
        it["err"] = res.get("err", "")
        self._refresh_row_widget(it)
        if self.selected_idx == idx:
            self.show_item(idx)

    # --- Azioni utente ---

    def clear_all(self) -> None:
        self.items.clear()
        self.typ_state.clear()
        self.selected_idx = -1
        self.typ_bar.set_visible(False)
        self.prog_card.set_visible(False)
        self.queue_count_chip.set_label("0")
        self.status_lbl.set_text("Aggiungi immagini o carica un file .typ per iniziare la conversione in CeTZ.")
        self._guard_ui = True
        self.code_buf.set_text("")
        self._guard_ui = False
        self.pic_orig.set_filename(None)
        self.pic_cetz.set_filename(None)
        while child := self.queue_list.get_first_child():
            self.queue_list.remove(child)

    def on_code_changed(self, _buf: Gtk.TextBuffer) -> None:
        if self._guard_ui or self.selected_idx < 0 or self.selected_idx >= len(self.items):
            return
        it = self.items[self.selected_idx]
        it["code"] = self.code_buf.get_text(self.code_buf.get_start_iter(), self.code_buf.get_end_iter(), True)
        it["dirty"] = True

    def on_keep_orig_toggled(self, btn: Gtk.ToggleButton) -> None:
        if self._guard_ui or self.selected_idx < 0 or self.selected_idx >= len(self.items):
            return
        it = self.items[self.selected_idx]
        it["keep_orig"] = btn.get_active()
        self._refresh_row_widget(it)
        self.show_item(self.selected_idx)

    def on_copy_code(self, *_: Any) -> None:
        if self.selected_idx < 0 or self.selected_idx >= len(self.items):
            return
        code = self.code_buf.get_text(self.code_buf.get_start_iter(), self.code_buf.get_end_iter(), True)
        if not code.strip():
            return
        Gdk.Display.get_default().get_clipboard().set(code)
        self.toast_cb("Codice CeTZ copiato negli appunti")

    def on_cancel_clicked(self, *_: Any) -> None:
        self._cancel_ev.set()
        self.toast_cb("Interruzione richiesta al termine della figura corrente…")

    def on_pick_images(self, *_: Any) -> None:
        init_dir = self.screenshots_dir if self.screenshots_dir.exists() else Path.home()
        dlg = Gtk.FileDialog(title="Scegli immagini da convertire in CeTZ", initial_folder=Gio.File.new_for_path(str(init_dir)))
        f_img = Gtk.FileFilter()
        f_img.set_name("Immagini (PNG, JPG, WebP)")
        for pat in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            f_img.add_pattern(pat)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f_img)
        dlg.set_filters(filters)
        dlg.open_multiple(self.win, None, self._on_images_picked)

    def _on_images_picked(self, dlg: Gtk.FileDialog, res: Gio.AsyncResult) -> None:
        try:
            files = dlg.open_multiple_finish(res)
        except Exception:
            return
        paths = [Path(f.get_path()) for f in files if f.get_path()]
        if paths:
            self._enqueue_images(paths)

    def _enqueue_images(self, paths: list[Path]) -> None:
        if self.typ_state:
            self.clear_all()
        start_idx = len(self.items)
        for offset, p in enumerate(paths):
            it = {
                "path": p,
                "name": p.name,
                "stem": f"cetz_{int(time.time())}_{start_idx + offset}",
                "st": "In coda",
                "code": "",
                "png": "",
                "ok": False,
                "err": "",
                "keep_orig": False,
                "dirty": False,
            }
            self.items.append(it)
            self._append_queue_row(it)
        if self.selected_idx < 0 and self.items:
            self.select_index(0)
        self.on_process_queue()

    def on_process_queue(self, *_: Any) -> None:
        if self._busy:
            return
        pending = [
            i for i, it in enumerate(self.items) if it["st"] in ("In coda", "Errore agy", "Errore gemini-api", "Errore Typst")
        ]
        if not pending:
            self.toast_cb("Nessuna immagine in coda da elaborare")
            return

        model = self.current_model()
        backend = self.current_backend()
        prompt_text = self.prompt_file.read_text(encoding="utf-8").strip()
        skip_ns = self.chk_skip_non_study.get_active()
        self._set_busy(True, total=len(pending), label=f"CeTZ ({backend} · {model})")

        def worker() -> None:
            for start in range(0, len(pending), self.batch_size):
                if self._cancel_ev.is_set():
                    break
                batch_idxs = pending[start : start + self.batch_size]
                dirs = list({Path(self.items[i]["path"]).parent for i in batch_idxs if self.items[i].get("path")})
                try:
                    sess_cm = create_llm_session(self.gen, model=model, add_dirs=dirs, backend=backend)
                except Exception as exc:
                    GLib.idle_add(self.toast_cb, str(exc))
                    break
                with sess_cm as sess:
                    for i in batch_idxs:
                        if self._cancel_ev.is_set():
                            break
                        it = self.items[i]
                        GLib.idle_add(self._update_item, i, {"status": "Elaborazione…"})
                        res = convert_single_image(
                            general=self.gen,
                            agy_bin=self.agy_bin,
                            typst_bin=self.typst_bin,
                            model=model,
                            prompt_text=prompt_text,
                            img_path=Path(it["path"]),
                            out_dir=self.backup_dir,
                            stem=it["stem"],
                            session=sess,
                            backend=backend,
                            skip_non_study=skip_ns,
                            max_retries=self.max_retries,
                            status_cb=lambda s, idx=i: GLib.idle_add(self._update_item, idx, {"status": s}),
                        )

                        def on_one(idx: int = i, r: dict[str, Any] = res) -> None:
                            self._run_done += 1
                            if r.get("duration"):
                                self._run_durations.append(float(r["duration"]))
                            self._update_item(idx, r)
                            self._render_progress()

                        GLib.idle_add(on_one)

            def done() -> None:
                msg = "Interrotto" if self._cancel_ev.is_set() else f"Completate {self._run_done}/{self._run_total} figure"
                self._set_busy(False, label=msg)
                self.toast_cb(msg)

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def on_rerun_clicked(self, *_: Any) -> None:
        idx = self.selected_idx
        if idx < 0 or idx >= len(self.items):
            return
        code = self.code_buf.get_text(self.code_buf.get_start_iter(), self.code_buf.get_end_iter(), True).strip()
        if not code:
            return
        self._update_item(idx, {"status": "Compilo…"})

        def worker() -> None:
            ok, pp, err = render_cetz_png(self.typst_bin, code, self.backup_dir, self.items[idx]["stem"])
            GLib.idle_add(
                self._update_item,
                idx,
                {"ok": ok, "status": "Pronto" if ok else "Errore Typst", "code": code, "png": str(pp) if ok else "", "err": "" if ok else err},
            )

        threading.Thread(target=worker, daemon=True).start()

    def on_ai_edit(self, *_: Any) -> None:
        if self._busy or self.selected_idx < 0 or self.selected_idx >= len(self.items):
            return
        idx = self.selected_idx
        it = self.items[idx]
        q = self.ent_ai.get_text().strip()
        cur_code = self.code_buf.get_text(self.code_buf.get_start_iter(), self.code_buf.get_end_iter(), True).strip()
        if cur_code and not q:
            return
        if not cur_code and not it.get("path"):
            return

        self.ent_ai.set_text("")
        model = self.current_model()
        backend = self.current_backend()
        prompt_text = self.prompt_file.read_text(encoding="utf-8").strip()
        self._set_busy(True, total=1, label=f"Modifica AI · {it['name']}")
        self._update_item(idx, {"status": "Modifica AI…"})

        def worker() -> None:
            try:
                if cur_code:
                    res = refine_cetz_code(
                        general=self.gen,
                        agy_bin=self.agy_bin,
                        typst_bin=self.typst_bin,
                        model=model,
                        current_code=cur_code,
                        instruction=q,
                        out_dir=self.backup_dir,
                        stem=it["stem"],
                        img_path=Path(it["path"]) if it.get("path") else None,
                        backend=backend,
                        max_retries=self.max_retries,
                    )
                else:
                    res = convert_single_image(
                        general=self.gen,
                        agy_bin=self.agy_bin,
                        typst_bin=self.typst_bin,
                        model=model,
                        prompt_text=prompt_text,
                        img_path=Path(it["path"]),
                        out_dir=self.backup_dir,
                        stem=it["stem"],
                        backend=backend,
                        skip_non_study=False,
                        extra_instruction=q,
                        max_retries=self.max_retries,
                    )
            except Exception as exc:
                res = {"ok": False, "status": f"Errore {backend}", "code": cur_code, "png": "", "err": str(exc)}

            def done() -> None:
                self._run_done = 1
                self._update_item(idx, res)
                self._set_busy(False, label="Modifica completata" if res["ok"] else res["status"])
                self.toast_cb("Figura aggiornata" if res["ok"] else f"Errore: {res['status']}")

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    # --- Conversione batch file .typ ---

    def on_pick_typ(self, *_: Any) -> None:
        dlg = Gtk.FileDialog(title="Scegli un file .typ da convertire")
        f_typ = Gtk.FileFilter()
        f_typ.set_name("File Typst (*.typ)")
        f_typ.add_pattern("*.typ")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f_typ)
        dlg.set_filters(filters)
        dlg.open(self.win, None, self._on_typ_picked)

    def _on_typ_picked(self, dlg: Gtk.FileDialog, res: Gio.AsyncResult) -> None:
        try:
            gfile = dlg.open_finish(res)
        except Exception:
            return
        if not gfile or not gfile.get_path():
            return
        self._start_typ_conversion(Path(gfile.get_path()))

    def _start_typ_conversion(self, typ_path: Path) -> None:
        txt, lines, tasks = parse_typ_images(typ_path, self.screenshots_dir)
        if not tasks:
            self.toast_cb(f"Nessuna immagine trovata in {typ_path.name}")
            return

        auto_mode = self.mode_dd.get_selected() == 1
        model = self.current_model()
        backend = self.current_backend()
        prompt_text = self.prompt_file.read_text(encoding="utf-8").strip()
        skip_ns = self.chk_skip_non_study.get_active()
        self._set_busy(True, total=len(tasks), label=f"Conversione {typ_path.name}")

        def on_prog(done: int, tot: int, fname: str, dur: float | None) -> None:
            def upd() -> None:
                self._run_done = done
                self._run_total = tot
                self._run_label = f"{typ_path.name} · {fname}"
                if dur is not None:
                    self._run_durations.append(dur)
                self._render_progress()

            GLib.idle_add(upd)

        def worker() -> None:
            try:
                results = convert_typ_batches(
                    tasks,
                    general=self.gen,
                    agy_bin=self.agy_bin,
                    typst_bin=self.typst_bin,
                    model=model,
                    prompt_text=prompt_text,
                    out_dir=self.backup_dir,
                    backend=backend,
                    batch_size=self.batch_size,
                    skip_non_study=skip_ns,
                    max_retries=self.max_retries,
                    progress_fn=on_prog,
                    cancel_event=self._cancel_ev,
                )
            except Exception as exc:
                GLib.idle_add(self._set_busy, False, 0, f"Errore: {exc}")
                GLib.idle_add(self.toast_cb, str(exc))
                return

            def finish() -> None:
                if auto_mode and not self._cancel_ev.is_set():
                    sels = [(r["task"], r["code"] if r["ok"] else "", not r["ok"]) for r in results]
                    rep, kept = apply_typ_replacements(typ_path, txt, lines, sels)
                    self._set_busy(False, label=f"Salvato {typ_path.name}: {rep} sostituite, {kept} originali")
                    self.toast_cb(f".typ salvato: {rep} sostituite con CeTZ ({kept} originali)")
                else:
                    self._load_typ_review(typ_path, txt, lines, results)
                    self._set_busy(False, label=f"Pronte {len(results)} figure per revisione")
                    self.toast_cb(f"Revisione pronta: controlla le figure e premi Salva .typ")

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

    def _load_typ_review(self, typ_path: Path, txt: str, lines: list[str], results: list[dict[str, Any]]) -> None:
        self.clear_all()
        self.typ_state = {"path": typ_path, "txt": txt, "lines": lines}
        self.typ_bar.set_visible(True)
        self.code_exp.set_expanded(True)
        for r in results:
            t = r["task"]
            p = t[4]
            it = {
                "path": p,
                "name": f"L{t[0] + 1}: {p.name}",
                "stem": r["stem"],
                "st": r["status"],
                "code": r["code"],
                "png": r["png"],
                "ok": r["ok"],
                "err": r["err"],
                "keep_orig": not r["ok"],
                "dirty": False,
                "task": t,
            }
            self.items.append(it)
            self._append_queue_row(it)
        if self.items:
            self.select_index(0)

    def on_save_typ(self, *_: Any) -> None:
        if not self.typ_state or self._busy:
            return
        typ_path: Path = self.typ_state["path"]
        self._set_busy(True, total=len(self.items), label=f"Salvataggio {typ_path.name}…")

        def worker() -> None:
            selections = []
            for idx, it in enumerate(self.items):
                if not it.get("task"):
                    continue
                code = it["code"].strip()
                keep = bool(it.get("keep_orig"))
                if not keep and code and it.get("dirty"):
                    ok, pp, err = render_cetz_png(self.typst_bin, code, self.backup_dir, it["stem"])
                    GLib.idle_add(
                        self._update_item,
                        idx,
                        {"ok": ok, "status": "Pronto" if ok else "Errore Typst", "code": code, "png": str(pp) if ok else "", "err": "" if ok else err},
                    )
                    if not ok:
                        keep = True
                elif not it.get("ok"):
                    keep = True
                selections.append((it["task"], code, keep))

            rep, kept = apply_typ_replacements(
                typ_path, self.typ_state["txt"], self.typ_state["lines"], selections
            )

            def done() -> None:
                self._run_done = self._run_total
                msg = f"Salvato {typ_path.name}: {rep} CeTZ, {kept} originali (.bak creato)"
                self._set_busy(False, label=msg)
                self.toast_cb(msg)

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()
