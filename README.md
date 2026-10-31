# YouTube Transcript

A local-first CLI for downloading YouTube captions and, when no matching track exists, transcribing audio locally with `whisper.cpp`.

## Install on macOS or Linux

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git, then install the command from GitHub:

```bash
uv tool install --from git+https://github.com/shubinlab/youtube-transcript.git@v0.1.0 youtube-transcript-local
```

`uv` installs the Python CLI in an isolated tool environment. The application itself has no third-party Python runtime dependencies.

To update or remove it:

```bash
uv tool upgrade youtube-transcript-local
uv tool uninstall youtube-transcript-local
```

If the command is not on `PATH`, check `uv tool dir --bin` and add that directory to your shell's `PATH`.

The repository is released under the MIT license; see [LICENSE](LICENSE).

### YouTube captions

Install a current `yt-dlp` release. YouTube extraction also needs a supported JavaScript runtime and yt-dlp's EJS components; Deno 2 is the recommended runtime ([yt-dlp setup guide](https://github.com/yt-dlp/yt-dlp/wiki/EJS)). The current Homebrew `yt-dlp` formula installs Deno as a dependency ([formula](https://formulae.brew.sh/formula/yt-dlp)). On Linux, use a current official yt-dlp distribution or install `yt-dlp[default]` plus Deno.

On macOS with Homebrew:

```bash
brew install uv yt-dlp
```

On Linux, install `uv`, a current `yt-dlp` with EJS support, and Deno 2 using your distribution's packages or the upstream installation guides. The `yt-dlp` executable and Deno must both be on `PATH`.

### Optional local ASR fallback

Local ASR is only needed when YouTube has no matching caption track, or when you explicitly request `--force-asr`. Install FFmpeg and `whisper-cli` from the [whisper.cpp project](https://github.com/ggml-org/whisper.cpp), then download a multilingual GGML model separately ([model setup](https://github.com/ggml-org/whisper.cpp#quick-start)). Model weights are not installed or uploaded by this tool.

On macOS with Homebrew:

```bash
brew install ffmpeg whisper.cpp
```

Select a local model with `WHISPER_MODEL=/path/to/ggml-small.bin` or pass `--model`. English-only models ending in `.en.bin` are rejected for Russian transcription. On Linux, install FFmpeg and whisper.cpp using your distribution or build whisper.cpp from its official repository.

### Install from a source checkout

```bash
git clone https://github.com/shubinlab/youtube-transcript.git
cd youtube-transcript
./install.sh
```

This creates a user-local symlink. Caption retrieval requires Python 3 and `yt-dlp`. Missing ASR tools or a model do not block installation; the ASR fallback reports what is missing when it is needed. Use `./uninstall.sh` to remove only the symlink.

## Use

```bash
youtube-transcript 'https://www.youtube.com/live/VIDEO_ID'
youtube-transcript -l en 'https://www.youtube.com/watch?v=VIDEO_ID' -o ./transcripts
```

The requested language defaults to Russian. The command prefers a manual YouTube track, then an original-language automatic track. It excludes live-chat and translated automatic tracks. If no suitable captions exist, it downloads the audio and runs local ASR.

Each run writes:

- `<video-id>.vtt`: the original timestamped caption track or local ASR output.
- `<video-id>.transcript.json`: source type, requested and selected language, track or model, video/audio durations, cue range, coverage warning, tool versions and VTT hash.

The raw VTT is retained as evidence. YouTube captions contain rolling, overlapping cues; this tool does not deduplicate or silently rewrite them. `--force-asr` creates an alternative local transcript, not a comparison report. Use a separate output directory to keep both versions:

```bash
youtube-transcript --force-asr 'https://www.youtube.com/watch?v=VIDEO_ID' -o ./local-asr
```

Disagreement between two ASR systems identifies passages to review; it does not establish which transcript is correct.

Existing outputs are preserved. Use `--force` only when you intend to replace that video's VTT and metadata. Same-video runs are serialized.

## Privacy and limitations

- Caption and audio retrieval contact YouTube through `yt-dlp`.
- ASR runs locally with `whisper-cli`; no audio or transcript is sent to a cloud ASR service.
- This tool does not read browser cookies, authenticate to private videos, bypass access restrictions, translate captions, or identify speakers.
- Automatic captions and local ASR can both be wrong. The JSON sidecar records provenance and basic timeline coverage; it does not measure recognition accuracy.
- Active or upcoming live streams are rejected. Use an archived video.

## Development

```bash
python -m unittest discover -s . -p 'test_*.py' -v
bash -n install.sh uninstall.sh
```

CI tests Python 3.10 and 3.14 on Linux and macOS. Tests use local fixtures and do not download media or model weights.
