#!/bin/bash
# Double-clic : met à jour l'outil, puis ouvre la page des stats TikTok.
cd "$(dirname "$0")"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  echo "Il faut Python 3 (gratuit). Installe-le depuis la page qui s'ouvre, puis relance Stats TikTok."
  open "https://www.python.org/downloads/macos/"
  read -r -p "Appuie sur Entrée pour fermer."
  exit 1
fi
python3 lanceur.py "$@"
read -r -p "Appuie sur Entrée pour fermer."
