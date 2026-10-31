#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd -P)
bin_dir=${XDG_BIN_HOME:-"$HOME/.local/bin"}
target="$bin_dir/youtube-transcript"

if [[ ! -L "$target" ]]; then
    printf 'No installed symlink found at %s\n' "$target"
    exit 0
fi
target_source=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$target")
if [[ "$target_source" != "$script_dir/youtube_transcript.py" ]]; then
    printf 'Refusing to remove symlink that points elsewhere: %s\n' "$target" >&2
    exit 1
fi
rm "$target"
printf 'Removed: %s\n' "$target"
