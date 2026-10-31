# Changelog

## 0.2.0

- Add a one-command user-local installer for Linux and macOS; it bootstraps only missing caption prerequisites.
- Bundle yt-dlp in the isolated CLI environment and expose a read-only `--check` readiness report.
- Add an optional local Deno runtime only when no supported Deno or Node.js runtime is available.
- Move automated tests into `tests/` and refresh the project README, install guide, and contributor instructions.
- Require Python 3.11 or newer.

## 0.1.0

- Initial public release of the local-first YouTube transcript CLI.
