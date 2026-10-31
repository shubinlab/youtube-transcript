#!/usr/bin/env bash
set -euo pipefail

uv_bin=$(command -v uv || true)
if [[ -z "$uv_bin" ]]; then
    for candidate in "${XDG_BIN_HOME:-$HOME/.local/bin}/uv" "$HOME/.local/bin/uv"; do
        if [[ -x "$candidate" ]]; then
            uv_bin=$candidate
            break
        fi
    done
fi
if [[ -z "$uv_bin" ]]; then
    printf 'youtube-transcript: the installation helper is missing; nothing was removed.\n' >&2
    exit 1
fi

installed_tools=$("$uv_bin" tool list 2>/dev/null || true)
if ! printf '%s\n' "$installed_tools" | grep -Eq '^youtube-transcript-local v'; then
    printf 'youtube-transcript is not installed through the user-local installer.\n'
    exit 0
fi

tool_bin=$("$uv_bin" tool dir --bin)
launcher="$tool_bin/youtube-transcript"
if [[ -e "$launcher" || -L "$launcher" ]]; then
    if [[ ! -L "$launcher" ]]; then
        printf 'youtube-transcript: command path is not a managed link; left it untouched.\n' >&2
        exit 1
    fi
    link_target=$(readlink "$launcher")
    case "$link_target" in
        /*) ;;
        *) link_target="$(dirname "$launcher")/$link_target" ;;
    esac
    link_parent=$(cd "$(dirname "$link_target")" 2>/dev/null && pwd -P) || {
        printf 'youtube-transcript: command link is broken; left it untouched.\n' >&2
        exit 1
    }
    managed_bin=$(cd "$("$uv_bin" tool dir)/youtube-transcript-local/bin" 2>/dev/null && pwd -P) || {
        printf 'youtube-transcript: managed package directory is missing; left it untouched.\n' >&2
        exit 1
    }
    if [[ "$link_parent" != "$managed_bin" || "${link_target##*/}" != "youtube-transcript" ]]; then
        printf 'youtube-transcript: command link points outside the managed package; left it untouched.\n' >&2
        exit 1
    fi
fi

"$uv_bin" tool uninstall youtube-transcript-local >/dev/null
printf 'Removed youtube-transcript. The shared download cache was left in place.\n'
