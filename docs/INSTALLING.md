# Install and recover

## What the installer does

The macOS/Linux installer stays in the current user's account. It:

1. Downloads the latest release archive directly from GitHub and verifies its SHA-256 checksum; Git is not required.
2. Installs the small `uv` runtime helper if it is missing. It does not edit shell startup files.
3. Reuses Deno 2.3+ or Node.js 22+ when available. Otherwise it adds the official Deno runtime to the isolated application environment.
4. Installs the CLI and its YouTube downloader in an isolated environment.
5. Runs `youtube-transcript --check` before reporting success.

It does not call `sudo`, install Homebrew or a Linux package manager, change system packages, or install local speech-recognition tools/model weights. Re-running the install command updates the CLI to the latest published release. A fresh install downloads the Python runtime if none is available and Deno (about 40 MB) only if a supported Deno or Node.js runtime is missing.

## Verify an installation

```bash
youtube-transcript --check
```

The check is read-only and reports caption readiness separately from optional local ASR readiness. It does not contact YouTube.

## Remove

```bash
curl -fsSL https://raw.githubusercontent.com/shubinlab/youtube-transcript/main/uninstall.sh | bash
```

This removes the isolated CLI environment. It leaves the shared `uv` helper and download cache in place.

## Optional local speech recognition

Only set up ASR if a video has no usable caption track or you want `--force-asr`. Install FFmpeg and `whisper-cli` from the official whisper.cpp project, then download a multilingual GGML model from its model instructions. Keep the model path explicit:

```bash
youtube-transcript --model /path/to/ggml-small.bin 'https://www.youtube.com/watch?v=VIDEO_ID'
```

The multilingual model is several hundred megabytes. The installer never downloads it. English-only models are rejected when requesting Russian. On macOS, Homebrew provides FFmpeg and whisper.cpp; Linux users should use trusted distribution packages or the official whisper.cpp build instructions.

## Advanced: install with uv directly

For an existing Python/uv development setup:

```bash
uv tool install git+https://github.com/shubinlab/youtube-transcript.git
```

This path does not bootstrap uv or prepare a missing JavaScript runtime. Use the installer above for a fresh machine.

## Supported systems

The automatic path supports macOS on Intel and Apple Silicon, plus Linux x86_64 and ARM64 with glibc 2.27 or newer when it needs to supply Deno. Existing supported Deno or Node.js installations can be reused. Windows and musl-based Linux are not supported by the automatic installer.
