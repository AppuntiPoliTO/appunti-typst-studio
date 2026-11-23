#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DST="$HOME/.local/bin"
ICON_DST="$HOME/.local/share/icons/hicolor/scalable/apps"
APP_DST="$HOME/.local/share/applications"

mkdir -p "$BIN_DST" "$ICON_DST" "$APP_DST"

chmod +x "$REPO_DIR/bin/appunti" "$REPO_DIR/bin/appunti-gui" "$REPO_DIR/bin/cetz-screenshot" \
         "$REPO_DIR/core/studio_gui.py" "$REPO_DIR/core/discover.py" "$REPO_DIR/core/parse_mineru.py" \
         "$REPO_DIR/core/figures.py" "$REPO_DIR/core/transcribe.py" "$REPO_DIR/core/insert_chapter.py" \
         "$REPO_DIR/core/merge_chapter.py"

ln -sf "$REPO_DIR/bin/appunti" "$BIN_DST/appunti"
ln -sf "$REPO_DIR/bin/appunti-gui" "$BIN_DST/appunti-gui"
ln -sf "$REPO_DIR/bin/cetz-screenshot" "$BIN_DST/cetz-screenshot"

# Installa l'icona SVG vettoriale e la voce desktop GNOME
cp "$REPO_DIR/assets/appunti-typst-studio.svg" "$ICON_DST/appunti-typst-studio.svg"
sed "s|^Exec=.*|Exec=$BIN_DST/appunti-gui|" "$REPO_DIR/assets/appunti-typst-studio.desktop" > "$APP_DST/appunti-typst-studio.desktop"

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DST" 2>/dev/null || true
fi

if [ ! -f "$REPO_DIR/config.toml" ]; then
  cp "$REPO_DIR/config.example.toml" "$REPO_DIR/config.toml"
  echo "Creato $REPO_DIR/config.toml da config.example.toml — personalizza i percorsi delle materie."
fi

if [[ "${1:-}" == "--with-mineru" ]]; then
  echo "Installazione ambiente Python 3.12 + MinerU 4.x ONNX..."
  uv venv --python 3.12 "$REPO_DIR/.venv-mineru"
  uv pip install --python "$REPO_DIR/.venv-mineru/bin/python" -U mineru
  "$REPO_DIR/.venv-mineru/bin/mineru-models-download" MinerU-4_models_onnx --source huggingface
fi

echo "Installazione completata! Avvia con: appunti-gui (oppure cerca 'Appunti Typst Studio')."
