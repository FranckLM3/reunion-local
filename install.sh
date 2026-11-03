#!/usr/bin/env bash
# Installation de Réunion (macOS 15+, Apple Silicon). Relançable sans risque : sert aussi à mettre à jour.
set -euo pipefail
APP="$(cd "$(dirname "$0")" && pwd)"
CONF="$HOME/.config/reunion"
etape() { printf '\n\033[1m%s\033[0m\n' "$*"; }

echo "Installation de Réunion : environ 15 minutes et 3 Go la première fois (fichiers de transcription)."

[[ "$(uname -m)" == "arm64" ]] || { echo "Mac Apple Silicon (M1 ou plus récent) requis."; exit 1; }
MAJ=$(sw_vers -productVersion | cut -d. -f1)
(( MAJ >= 15 )) || { echo "macOS 15 minimum pour capter le micro."; exit 1; }
if ! xcode-select -p >/dev/null 2>&1; then
  xcode-select --install || true
  echo "Une fenêtre installe les outils de développement d'Apple. Relance ./install.sh quand elle a fini."
  exit 1
fi
if ! command -v brew >/dev/null; then
  echo "Homebrew manque. Installe-le avec la commande ci-dessous, puis relance ./install.sh :"
  echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  exit 1
fi

etape "1/4 ffmpeg et Python"
command -v ffmpeg >/dev/null || brew install ffmpeg
# pyannote 4 demande Python 3.10 ou plus (celui de macOS est en 3.9).
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    PY="$(command -v "$c")"
    break
  fi
done
if [[ -z "$PY" ]]; then
  brew install python@3.12
  PY="$(brew --prefix)/bin/python3.12"
fi
echo "Python : $PY"

etape "2/4 Enregistreur et app de la barre des menus"
# Nouveau fichier plutôt qu'écrasement : macOS tue un binaire signé modifié sur place.
rm -f "$APP/meetrec"
swiftc -O -parse-as-library -o "$APP/meetrec" "$APP/meetrec.swift"
"$APP/build_app.sh"

etape "3/4 Bibliothèques Python (3 à 5 minutes)"
"$PY" -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --quiet --upgrade pip
# sherpa-onnx : séparation des voix sans compte ni clé (méthode par défaut). pyannote (plus de 1 Go) : seulement
# si une clé d'accès est déjà rangée ; sinon la page propose de l'installer quand la clé est collée.
"$APP/.venv/bin/pip" install mlx-whisper sherpa-onnx soundfile recurring-ical-events
if [[ -n "${HF_TOKEN:-}" || -s "$CONF/hf_token" ]]; then
  "$APP/.venv/bin/pip" install "pyannote.audio>=4"
fi

[[ -f "$APP/vocabulaire.txt" ]] || cp "$APP/vocabulaire.exemple.txt" "$APP/vocabulaire.txt"
chmod +x "$APP/reunion.sh" "$APP/transcribe.py"
mkdir -p "$HOME/.local/bin"
ln -sf "$APP/reunion.sh" "$HOME/.local/bin/reunion"
# Commande /compte-rendu de Claude Code : seulement si vous le demandez.
if [[ ! -e "$HOME/.claude/skills/compte-rendu" ]]; then
  read -rp "Installer la commande /compte-rendu pour Claude Code ? [o/N] " R < /dev/tty || R=""
  if [[ "$R" == [oO]* ]]; then
    mkdir -p "$HOME/.claude/skills"
    ln -s "$APP/.claude/skills/compte-rendu" "$HOME/.claude/skills/compte-rendu"
  fi
fi

mkdir -p -m 700 "$CONF"

# Aucun compte ni clé n'est demandé : les fichiers de transcription et de séparation des voix sont publics.
etape "4/4 Fichiers de transcription et de séparation des voix (environ 2 Go, une seule fois)"
if ! "$APP/.venv/bin/python" "$APP/transcribe.py" --telecharger \
    || ! "$APP/.venv/bin/python" "$APP/transcribe.py" --telecharger-separation; then
  echo "Téléchargement incomplet (réseau). Pas bloquant : Réunion le reprend à son lancement."
fi

etape "Installé"
echo "Ouvre ~/Applications/Réunion.app : une icône d'onde apparaît dans la barre des menus."
echo "Au premier lancement, la page vous guide : votre nom, le micro, le son de l'ordinateur."
echo "Ne déplace pas ce dossier ($APP) : l'app s'en sert."
