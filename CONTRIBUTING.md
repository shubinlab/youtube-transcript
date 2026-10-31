# Contributing

Bug reports and pull requests are welcome. For transcript bugs, include the video platform URL only when it is public and safe to share; otherwise provide a sanitized fixture or describe the caption-track metadata. Never attach cookies, private URLs, or personal transcripts.

## Development setup

Python 3.11+ is required. From the repository root:

```bash
python -m pip install -e '.[js-runtime]'
python -m unittest discover -s tests -p 'test_*.py' -v
bash -n install.sh uninstall.sh
```

The suite uses local fixtures and fake installers; it does not download video or model weights. A public-video smoke test may be run manually after tests, but keep its outputs outside the repository.

## Pull requests

- Keep the captions-first behavior and raw YouTube VTT intact.
- Preserve existing outputs unless the caller explicitly uses `--force`.
- Add a focused regression test for changed behavior.
- Keep local ASR optional and never add implicit model downloads.
- Update the README or installation guide when user-facing behavior changes.
