#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_ROOT"

if [ ! -x .venv-desktop/bin/python ]; then
    python3 -m venv .venv-desktop
fi
.venv-desktop/bin/python -m pip install -r requirements-desktop.txt
.venv-desktop/bin/python -m PyInstaller \
    --noconfirm \
    --clean \
    --windowed \
    --onefile \
    --name VideoArchive \
    --distpath output/linux \
    --workpath build/linux \
    --specpath build/linux \
    --add-data "$PROJECT_ROOT/static:static" \
    desktop_app.py

printf 'Готово: %s\n' "$PROJECT_ROOT/output/linux/VideoArchive"
