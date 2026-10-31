#!/usr/bin/env bash
set -euo pipefail

readonly TOOL_NAME="youtube-transcript-local"
readonly COMMAND_NAME="youtube-transcript"
readonly RELEASE_URL="https://github.com/shubinlab/youtube-transcript/releases/latest/download"
readonly UV_INSTALLER_URL="https://astral.sh/uv/install.sh"
readonly USER_BIN="$HOME/.local/bin"

fail() {
    printf 'youtube-transcript: %s\n' "$1" >&2
    exit 1
}

case "$(uname -s)" in
    Linux|Darwin) ;;
    *) fail "This installer supports Linux and macOS." ;;
esac
case "$(uname -m)" in
    x86_64|aarch64|arm64) ;;
    *) fail "This CPU architecture is not supported by the automatic installer." ;;
esac

for program in curl bash cat readlink; do
    command -v "$program" >/dev/null 2>&1 || fail "Required system utility is missing: $program"
done

version_at_least() {
    local value=$1 required_major=$2 required_minor=$3 major minor
    [[ "$value" =~ ^v?([0-9]+)\.([0-9]+) ]] || return 1
    major=${BASH_REMATCH[1]}
    minor=${BASH_REMATCH[2]}
    (( major > required_major || (major == required_major && minor >= required_minor) ))
}

has_js_runtime() {
    local value
    if command -v deno >/dev/null 2>&1; then
        value=$(deno --version 2>/dev/null | sed -n '1s/^deno //p')
        version_at_least "$value" 2 3 && return 0
    fi
    if command -v node >/dev/null 2>&1; then
        value=$(node --version 2>/dev/null | head -n 1)
        version_at_least "$value" 22 0 && return 0
    fi
    return 1
}

needs_bundled_deno() {
    has_js_runtime && return 1
    [[ "$(uname -s)" == "Linux" ]] || return 0

    local libc_info libc_version
    libc_info=$(getconf GNU_LIBC_VERSION 2>/dev/null || true)
    if [[ ! "$libc_info" =~ ^glibc\ ([0-9]+\.[0-9]+)$ ]]; then
        fail "No supported Deno or Node.js runtime was found. Install Deno or Node.js 22+ for this Linux system."
    fi
    libc_version=${BASH_REMATCH[1]}
    if ! version_at_least "$libc_version" 2 27; then
        fail "The bundled runtime needs glibc 2.27 or newer. Install Deno or Node.js 22+ for this Linux system."
    fi
    return 0
}

temp_dir=$(mktemp -d "${TMPDIR:-/tmp}/youtube-transcript-install.XXXXXX")
trap 'rm -rf "$temp_dir"' EXIT

curl -fsSL "$RELEASE_URL/youtube-transcript-source.zip" -o "$temp_dir/source.zip" \
    || fail "Could not download the latest release. Check your connection and retry."
curl -fsSL "$RELEASE_URL/youtube-transcript-source.zip.sha256" -o "$temp_dir/source.zip.sha256" \
    || fail "Could not verify the downloaded release."
IFS=' ' read -r expected_hash _ < "$temp_dir/source.zip.sha256"
if [[ ! "$expected_hash" =~ ^[[:xdigit:]]{64}$ ]]; then
    fail "The release checksum file is invalid."
fi
if command -v sha256sum >/dev/null 2>&1; then
    checksum_line=$(sha256sum "$temp_dir/source.zip")
elif command -v shasum >/dev/null 2>&1; then
    checksum_line=$(shasum -a 256 "$temp_dir/source.zip")
else
    fail "A SHA-256 checker is not available on this system."
fi
actual_hash=${checksum_line%% *}
[[ "$expected_hash" == "$actual_hash" ]] || fail "The downloaded release failed its integrity check."

uv_bin=$(command -v uv || true)
if [[ -z "$uv_bin" ]]; then
    mkdir -p "$USER_BIN"
    if [[ -x "$USER_BIN/uv" ]]; then
        uv_bin="$USER_BIN/uv"
    elif [[ -e "$USER_BIN/uv" || -L "$USER_BIN/uv" ]]; then
        fail "An item already occupies $USER_BIN/uv and was left untouched."
    else
        curl -fsSL "$UV_INSTALLER_URL" -o "$temp_dir/install-uv.sh" \
            || fail "Could not download the installer helper. Check your connection and retry."
        if ! UV_INSTALL_DIR="$USER_BIN" UV_NO_MODIFY_PATH=1 sh "$temp_dir/install-uv.sh" \
            >"$temp_dir/install-uv.log" 2>&1; then
            cat "$temp_dir/install-uv.log" >&2
            fail "Could not install the user-local runtime helper."
        fi
        uv_bin="$USER_BIN/uv"
    fi
fi
[[ -x "$uv_bin" ]] || fail "The runtime helper was not found after installation."

tool_bin=$("$uv_bin" tool dir --bin) || fail "Could not locate the user command directory."
installed_tools=$("$uv_bin" tool list 2>/dev/null || true)
launcher="$tool_bin/$COMMAND_NAME"
tool_installed=false
if printf '%s\n' "$installed_tools" | grep -Eq "^${TOOL_NAME} v"; then
    tool_installed=true
fi
if [[ -e "$launcher" || -L "$launcher" ]]; then
    if [[ "$tool_installed" != true || ! -L "$launcher" ]]; then
        fail "An existing command already occupies $launcher. Move or uninstall it first; it was left untouched."
    fi
    link_target=$(readlink "$launcher")
    case "$link_target" in
        /*) ;;
        *) link_target="$(dirname "$launcher")/$link_target" ;;
    esac
    link_parent=$(cd "$(dirname "$link_target")" 2>/dev/null && pwd -P) \
        || fail "The existing command link is broken; it was left untouched."
    managed_bin=$(cd "$("$uv_bin" tool dir)/$TOOL_NAME/bin" 2>/dev/null && pwd -P) \
        || fail "The existing package environment is missing; the command was left untouched."
    if [[ "$link_parent" != "$managed_bin" || "${link_target##*/}" != "$COMMAND_NAME" ]]; then
        fail "The existing command link points outside this package; it was left untouched."
    fi
fi

install_args=(tool install --force)
if needs_bundled_deno; then
    install_args+=(--with deno)
fi
install_args+=(--from "$temp_dir/source.zip" "$TOOL_NAME")
printf 'Preparing YouTube caption support...\n'
if ! "$uv_bin" "${install_args[@]}" >"$temp_dir/tool-install.log" 2>&1; then
    cat "$temp_dir/tool-install.log" >&2
    fail "Installation did not complete. No system packages were changed."
fi

[[ -x "$launcher" ]] || fail "The command launcher was not created at $launcher."
if ! "$launcher" --check >"$temp_dir/readiness.log" 2>&1; then
    cat "$temp_dir/readiness.log" >&2
    fail "Caption support could not be verified. See the installation and recovery guide."
fi

printf '\nInstalled. Run: %s <YouTube URL>\n' "$COMMAND_NAME"
case ":${PATH:-}:" in
    *":$tool_bin:"*) ;;
    *) printf 'Add %s to PATH to run the command from any directory.\n' "$tool_bin" ;;
esac
