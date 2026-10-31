#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd -P)
bin_dir=${XDG_BIN_HOME:-"$HOME/.local/bin"}
target="$bin_dir/youtube-transcript"

for program in python3 yt-dlp; do
    if ! command -v "$program" >/dev/null 2>&1; then
        printf 'Missing required program: %s\n' "$program" >&2
        exit 1
    fi
done

mkdir -p "$bin_dir"
if [[ -L "$target" ]]; then
    target_source=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$target")
    if [[ "$target_source" == "$script_dir/youtube_transcript.py" ]]; then
        printf 'Already installed: %s\n' "$target"
        exit 0
    fi
    printf 'Refusing to replace existing symlink: %s\n' "$target" >&2
    exit 1
fi
if [[ -e "$target" ]]; then
    printf 'Refusing to overwrite existing file: %s\n' "$target" >&2
    exit 1
fi

chmod +x "$script_dir/youtube_transcript.py"
ln -s "$script_dir/youtube_transcript.py" "$target"
printf 'Installed: %s\n' "$target"
if command -v ffmpeg >/dev/null 2>&1 \
    && command -v whisper-cli >/dev/null 2>&1 \
    && PYTHONPATH="$script_dir" python3 -c 'from youtube_transcript import resolve_model; resolve_model()' >/dev/null 2>&1; then
    printf 'Local ASR fallback is ready.\n'
else
    printf 'Captions mode is ready. For local ASR fallback install ffmpeg, whisper-cli, and a multilingual model.\n'
fi
if [[ ":$PATH:" != *":$bin_dir:"* ]]; then
    printf 'Add %s to PATH to call youtube-transcript by name.\n' "$bin_dir"
fi
