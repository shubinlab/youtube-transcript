# Security policy

The one-command installer downloads the CLI from this repository and its isolated Python/runtime dependencies from their upstream package sources. Review `install.sh` before running it if you do not trust the repository. The installer does not invoke a system package manager, request administrator access, or modify shell startup files.

The CLI runs locally. It uses the bundled downloader for YouTube retrieval and invokes `ffmpeg` and `whisper-cli` only for optional local ASR. It does not accept browser cookies or upload audio for ASR.

Please report a security issue through GitHub's private vulnerability reporting for this repository. Do not include private video URLs, cookies, access tokens, or personal transcripts in a public issue.
