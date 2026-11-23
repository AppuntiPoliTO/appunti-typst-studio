# Appunti Typst Studio

<p align="center">
  <img src="assets/appunti-typst-studio.svg" width="96" height="96" alt="Appunti Typst Studio Icon"/>
</p>

Suite unificata (CLI + GUI nativa **GTK 4 / Libadwaita**) che fonde:
1. **Pipeline Appunti Typst** — trasforma slide PDF + trascrizioni `.txt` (o audio/video) in capitoli Typst pronti per **LocalTypst**, con parsing locale **MinerU 4.x ONNX** (OCR + formule + ritagli figure), rilevamento automatico PDF corrotti/scansionati (`--force-ocr`), anteprima live incrementale e controllo visivo.
2. **CeTZ Studio** — editor/convertitore interattivo da immagini, screenshot o interi file `.typ` (`#image(...)`) a grafica vettoriale **CeTZ (`@preview/cetz:0.3.4`)**, con anteprima affiancata Originale/CeTZ, modifica AI iterativa, compilazione live (`Rerun`) e revisione guidata.

---

## Funzionalità Principali

- **Interfaccia unificata Libadwaita (`appunti-gui`)**:
  - **Scheda Appunti**: selezione automatica capitoli, wizard **Nuova Materia (`+`)**, Drag & Drop di `.txt`/audio/video, checkbox per singola parte (`Tutte / Pronte / Nessuna`), banner automatico per PDF che richiedono `--force-ocr`, modalità figure (**Ibrida / Tutto CeTZ / Solo ritagli MinerU**), scelta backend (**`agy` CLI / Gemini REST API**), sezione **Figure del capitolo** con ponte diretto **"Modifica in CeTZ Studio"**, stepper 7 fasi, barra progressi reale + stima **ETA**, log colorato e apertura diretta in **LocalTypst**.
  - **Scheda CeTZ Studio**: scelta backend & modello Gemini, Drag & Drop di immagini o `.typ`, coda immagini, convertitore batch `.typ` (*Revisione guidata* o *Automatica*), confronto fianco a fianco Originale/CeTZ, codice modificabile + `Rerun` + `Modifica AI`, salvataggio diretto live su LocalTypst per le figure di progetto e copia nativa in clipboard.
- **Libreria macro CeTZ condivise (`templates/cetz-macros.typ`)**: `axes-2d`, `axes-3d`, `dim-line`, `ground-hatch`, `center-line`, `force-vec`, `cyl-3d` automaticamente copiata in ogni progetto Typst.
- **Dual-Backend LLM (`core/agy_client.py`)**:
  - **`agy` CLI** (default): sessioni `stream-json` riutilizzabili a blocchi da 5 figure (zero lag su 8 GB RAM).
  - **`gemini-api`**: client REST API diretto (zero processi locali, ~15 MB RAM, funziona anche senza `agy` installato usando `GEMINI_API_KEY` o `~/.config/appunti/gemini.key`).
- **MinerU 4.x ONNX** su CPU con cache intelligente per PDF (0s sui passaggi successivi) e **live preview su LocalTypst** con scritture atomiche.
- **Shortcut Screenshot rapido (`cetz-screenshot`)**: cattura dagli appunti Wayland (`wl-paste`) o da `~/Screenshots`, converte in CeTZ via sessione isolata `stream-json`, copia in clipboard e apre anteprima rapida con modifica AI.

---

## Modelli Gemini Utilizzati

Tutti i modelli sono centralizzati in [`core/models.py`](core/models.py) e configurabili in `config.toml`:

| Ruolo | Chiave `config.toml` | Modello Predefinito | Backend |
|---|---|---|---|
| Redazione appunti Typst | `model_notes` | `gemini-4-hydrogen-medium` | `agy` CLI |
| Figure CeTZ (pipeline) + `--check` | `model_figures` | `gemini-4-hydrogen-medium` | `agy` CLI (`stream-json`) |
| CeTZ Studio (immagini / `.typ`) | `model_cetz_studio` | `gemini-4-hydrogen-high` | `agy` CLI (`stream-json`) |
| Shortcut screenshot CeTZ | `model_cetz_screenshot` | `gemini-4-hydrogen-medium` | `agy` CLI (`stream-json`) |
| Trascrizione audio/video (principale) | `gemini_transcribe_model` | `gemini-3.5-transcribe` | Gemini REST API (`gemini.key`) |
| Trascrizione audio/video (fallback) | `model_transcribe` | `gemini-4-hydrogen-medium` | `agy` CLI |

Nella scheda **CeTZ Studio** puoi cambiare modello al volo tra le serie **Gemini 4 Hydrogen**, **Gemini 4 Neon**, **Gemini 4 Argon** e **Gemini 3.8 Flash**.

---

## Installazione Rapida (Linux / Fedora)

### Prerequisiti
- Python 3.11+ con `PyGObject`, GTK 4 e Libadwaita (`python3-gobject`, `gtk4`, `libadwaita`)
- `typst`, `pdftoppm`, `pdftotext` (`poppler-utils`), `ffmpeg`
- `agy` CLI autenticato (`antigravity.google`)
- `uv` (opzionale, per installare MinerU 4.x ONNX in un venv isolato)

```bash
git clone https://github.com/AppuntiPoliTO/appunti-typst-studio.git
cd appunti-typst-studio

# Installa binari, icona SVG, voce desktop e config.toml
./install.sh

# (Consigliato) Installa anche MinerU 4.x ONNX + modelli locali OCR/layout
./install.sh --with-mineru
```

Poi modifica `config.toml` con i percorsi delle tue materie e della cartella `LocalTypst/projects/`.

---

## Utilizzo CLI & GUI

```bash
appunti-gui                           # Apre la GUI unificata (Appunti + CeTZ Studio)
appunti cetz                          # Apre direttamente la scheda CeTZ Studio
appunti ecm 12                        # Apre la GUI e avvia la generazione del Cap. 12
appunti ecm 12 --part 1,3             # Genera solo parti 1 e 3
appunti ecm 12 --force-ocr            # Forza OCR MinerU completo (--ocr-mode ocr)
appunti ecm 12 --no-mineru            # Usa PDF crudo + pdftoppm
appunti ecm 12 --no-gui               # Solo terminale (headless)
appunti merge ecm 12                  # Unisce _parts/<slug>/p*.typ in <slug>.typ
cetz-screenshot                       # Converte l'ultimo screenshot/clipboard in CeTZ
```

---

## Architettura per Integrazione in LocalTypst

Il progetto è progettato per essere integrato facilmente come backend/sidecar dentro **LocalTypst** (es. Tauri / Rust / React):
- **Core disaccoppiato dalla GUI**: tutta la logica è in `core/` (`discover.py`, `parse_mineru.py`, `figures.py`, `cetz_engine.py`, `transcribe.py`, `insert_chapter.py`, `merge_chapter.py`, `agy_client.py`).
- **Eventi macchina strutturati su `stdout`**: gli script emettono righe NDJSON `@@PROGRESS {...}` (`phase`, `status`, `done`, `total`, `item`, `duration`) che qualsiasi frontend (GTK, Tauri, web) può leggere per disegnare barre di avanzamento ed ETA native.
- **Scritture atomiche**: `figs/_index.typ`, `<capitolo>.typ` e `main.typ` vengono aggiornati via file temporaneo + `replace()`, così il file watcher di LocalTypst ricompila live senza mai leggere file troncati.
- **Zero path hardcoded**: tutta la risoluzione passa da `core/paths.py` e `APPUNTI_CONFIG`.
