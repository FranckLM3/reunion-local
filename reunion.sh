#!/usr/bin/env bash
# reunion : ouvre l'interface locale (http://127.0.0.1:8765), ou relance une transcription.
# Usage : reunion                                  ouvre l'interface
#         reunion --transcrire <dossier> [options] relance la transcription seule (voir transcribe.py --help)
# Pour enregistrer : Réunion.app (barre des menus) ou l'interface.
set -euo pipefail

APP="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PY="$APP/.venv/bin/python"

if [[ "${1:-}" == "--transcrire" ]]; then
  shift
  exec "$PY" "$APP/transcribe.py" "$@"
fi
exec "$PY" "$APP/serveur.py"
