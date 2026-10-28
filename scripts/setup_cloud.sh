#!/usr/bin/env bash
# Debian cloud workspace; never changes system packages or application files.
set -euo pipefail
cd /workspace/Word_Lookuper
tools_dir=/workspace/.word-lookup-tools
mkdir -p "$tools_dir/apt/lists/partial" "$tools_dir/debs" "$tools_dir/root" "$tools_dir/uv-cache"

# The managed Python's Tk 9 lacks Xft fonts here. System Tk 8.6 renders Ukrainian.
/usr/bin/python3 -c 'import tkinter; assert tkinter.TkVersion >= 8.6'
command -v tesseract >/dev/null
if [[ ! -x "$tools_dir/root/usr/bin/Xvfb" || ! -x "$tools_dir/root/usr/bin/xclip" || ! -f "$tools_dir/root/usr/include/python3.13/Python.h" ]]; then
    cat > "$tools_dir/apt/sources.list" <<'EOF'
deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian trixie main
EOF
    apt_opts=(-o "Dir::Etc::sourcelist=$tools_dir/apt/sources.list"
              -o Dir::Etc::sourceparts=- -o "Dir::State::lists=$tools_dir/apt/lists")
    apt-get "${apt_opts[@]}" update
    (cd "$tools_dir/debs" && apt-get "${apt_opts[@]}" download xvfb xclip libpython3.13-dev)
    for package in "$tools_dir"/debs/*.deb; do
        dpkg-deb -x "$package" "$tools_dir/root"
    done
fi
export UV_CACHE_DIR="$tools_dir/uv-cache"
export CC=gcc
export CFLAGS="-I$tools_dir/root/usr/include/python3.13 -I$tools_dir/root/usr/include"
uv venv --python /usr/bin/python3 --allow-existing .venv
uv pip sync --python .venv/bin/python requirements.lock
.venv/bin/python -c 'import tkinter, PIL, pytesseract, requests, genanki; print("Dependencies ready; Tk", tkinter.TkVersion)'
