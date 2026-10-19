#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_ROOT"

BUILD_PYTHON=${VIDEOARCHIVE_BUILD_PYTHON:-python3}
BUILD_VENV=${VIDEOARCHIVE_BUILD_VENV:-.venv-desktop}

if [ ! -x "$BUILD_VENV/bin/python" ]; then
    "$BUILD_PYTHON" -m venv "$BUILD_VENV"
fi
"$BUILD_VENV/bin/python" -m pip install -r requirements-desktop-linux.txt
set -- \
    --noconfirm \
    --clean \
    --windowed \
    --onefile \
    --name VideoArchive \
    --distpath output/linux \
    --workpath build/linux \
    --specpath build/linux \
    --add-data "$PROJECT_ROOT/static:static"

# Qt's xcb platform plugin needs this library on some otherwise supported Linux desktops.
if [ -f /usr/lib64/libxcb-cursor.so.0 ]; then
    set -- "$@" --add-binary "/usr/lib64/libxcb-cursor.so.0:PySide6/Qt/lib"
fi

"$BUILD_VENV/bin/python" -m PyInstaller "$@" desktop_app.py

printf 'Готово: %s\n' "$PROJECT_ROOT/output/linux/VideoArchive"
