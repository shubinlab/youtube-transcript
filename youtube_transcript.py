#!/usr/bin/env python3
"""Fetch YouTube captions, falling back to local whisper.cpp ASR."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


TIME_RE = re.compile(
    r"^(?P<start>(?:\d{2,}:)?\d{2}:\d{2}\.\d{3})\s+-->\s+"
    r"(?P<end>(?:\d{2,}:)?\d{2}:\d{2}\.\d{3})(?:\s+.*)?$"
)
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")
LIVE_CHAT_KEYS = {"live_chat", "live-chat"}


class TranscriptError(Exception):
    """An expected failure with a user-facing explanation."""


def _language_root(value: str | None) -> str:
    if not value:
        return ""
    return value.lower().split("-", 1)[0].split("_", 1)[0]


def select_track(info: dict, language: str) -> tuple[str, str] | None:
    """Return (manual|automatic, language key), preferring original speech."""
    if not isinstance(info, dict):
        raise TranscriptError("YouTube caption metadata is not an object")
    if any(
        info.get(field) is not None and not isinstance(info.get(field), dict)
        for field in ("subtitles", "automatic_captions")
    ):
        raise TranscriptError("YouTube caption metadata has an invalid track list")
    requested = language.lower().replace("_", "-")
    language_root = _language_root(requested)
    manual = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    for tracks in (*manual.values(), *automatic.values()):
        if not isinstance(tracks, list) or any(not isinstance(track, dict) for track in tracks):
            raise TranscriptError("YouTube caption metadata has an invalid track entry")
    if any(not isinstance(key, str) for key in (*manual.keys(), *automatic.keys())):
        raise TranscriptError("YouTube caption metadata has an invalid language key")

    def usable(keys):
        return [key for key, tracks in keys.items() if key not in LIVE_CHAT_KEYS and tracks]

    manual_keys = usable(manual)
    auto_keys = usable(automatic)

    # Manual tracks in the requested language are preferred over YouTube ASR.
    for key in (requested, language_root, f"{language_root}-orig"):
        if key in manual_keys:
            return "manual", key
    manual_locales = sorted(
        key for key in manual_keys if key.startswith(language_root + "-") and not key.endswith("-orig")
    )
    if manual_locales:
        return "manual", manual_locales[0]

    # YouTube labels its source-language ASR track explicitly with -orig.
    original_key = f"{language_root}-orig"
    if original_key in auto_keys:
        return "automatic", original_key

    # An unmarked exact-language auto track is safe only when yt-dlp also
    # identifies the video's original language as the requested language.
    if language_root in auto_keys and _language_root(info.get("language")) == language_root:
        return "automatic", language_root
    return None


def video_duration_seconds(info: dict) -> float:
    if not isinstance(info, dict):
        raise TranscriptError("yt-dlp returned invalid video metadata")
    if info.get("is_live") is True or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise TranscriptError("Active or upcoming live streams are not supported; use an archived video")
    duration = info.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise TranscriptError("Video duration is unavailable; refusing an unbounded live stream")
    try:
        finite_duration = math.isfinite(float(duration))
    except (OverflowError, ValueError):
        finite_duration = False
    if not finite_duration or duration <= 0:
        raise TranscriptError("Video duration is invalid")
    return float(duration)


def validate_youtube_url(value: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise TranscriptError("Invalid YouTube URL") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
    if parsed.scheme not in {"https", "http"} or host not in allowed:
        raise TranscriptError("Input must be a YouTube URL")
    if host.endswith("youtu.be") and not parsed.path.strip("/"):
        raise TranscriptError("YouTube short URL has no video ID")
    if host.endswith("youtube.com") and parsed.path not in {"/watch", "/live", "/shorts", "/embed"} and not parsed.path.startswith(("/live/", "/shorts/", "/embed/")):
        raise TranscriptError("URL is not a YouTube video URL")
    return value


def _seconds(timestamp: str) -> float:
    parts = timestamp.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        if int(minutes) < 0 or not 0 <= float(seconds) < 60:
            raise TranscriptError("Subtitle timestamp has invalid minute/second values")
        return int(minutes) * 60 + float(seconds)
    hours, minutes, seconds = parts
    if not 0 <= int(minutes) < 60 or not 0 <= float(seconds) < 60:
        raise TranscriptError("Subtitle timestamp has invalid minute/second values")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _has_visible_text(lines: list[str]) -> bool:
    text = html.unescape(re.sub(r"<[^>]*>", "", " ".join(lines)))
    return any(ch.isprintable() and not ch.isspace() for ch in text)


def validate_vtt(path: Path, duration: float) -> tuple[int, float, float]:
    """Check VTT structure and timeline; return cue count, first start, last end."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise TranscriptError(f"Cannot read subtitle file: {exc}") from exc
    lines = text.splitlines()
    if not lines or not re.fullmatch(r"WEBVTT(?:[ \t].*)?", lines[0]):
        raise TranscriptError("Subtitle output is not a WebVTT file")

    cues: list[tuple[float, float]] = []
    blocks = re.split(r"\n[ \t]*\n", "\n".join(lines[1:]).strip())
    for block in blocks:
        block_lines = block.splitlines()
        if not block_lines or block_lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        timing_index = next(
            (index for index, line in enumerate(block_lines) if "-->" in line), None
        )
        if timing_index is None:
            continue
        match = TIME_RE.match(block_lines[timing_index].strip())
        if not match:
            raise TranscriptError("Subtitle cue has malformed timestamps")
        start = _seconds(match.group("start"))
        end = _seconds(match.group("end"))
        if end < start:
            raise TranscriptError("Subtitle cue ends before it starts")
        if cues and start < cues[-1][0]:
            raise TranscriptError("Subtitle cue timestamps are out of order")
        if end > duration + 2:
            raise TranscriptError("Subtitle timestamps extend beyond the video duration")
        if _has_visible_text(block_lines[timing_index + 1 :]):
            cues.append((start, end))

    if not cues:
        raise TranscriptError("Subtitle file has no timestamped cues")
    return len(cues), cues[0][0], cues[-1][1]


def _model_candidates() -> list[Path]:
    home = Path.home()
    return [
        home / ".local/share/voxtype/models/ggml-small.bin",
        home / ".cache/whisper.cpp/ggml-small.bin",
        home / ".local/share/voxtype/models/ggml-base.bin",
        home / ".cache/whisper.cpp/ggml-base.bin",
    ]


def resolve_model(explicit: str | None = None) -> Path:
    """Resolve an existing multilingual whisper.cpp model without downloading it."""
    requested = explicit or os.environ.get("WHISPER_MODEL")
    candidates = [Path(requested).expanduser()] if requested else _model_candidates()
    for path in candidates:
        if path.is_file() and path.stat().st_size > 0:
            if re.search(r"\.en(?:\.|$)", path.name):
                raise TranscriptError(f"English-only Whisper model cannot transcribe {path.name}")
            return path.resolve()
    if requested:
        raise TranscriptError(f"Multilingual Whisper model is missing or empty: {candidates[0]}")
    raise TranscriptError(
        "No local multilingual Whisper model found. Pass --model or set WHISPER_MODEL; "
        "the tool does not download model weights."
    )


def _run(args: list[str], label: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, text=True, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise TranscriptError(f"Required program is not installed: {args[0]}") from exc
    except OSError as exc:
        raise TranscriptError(f"Could not start {label}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        detail = detail[-1] if detail else f"exit status {result.returncode}"
        # Never print signed YouTube caption URLs or query-string credentials.
        detail = re.sub(r"https?://\S+", "[URL redacted]", detail)
        detail = re.sub(r"\?\S+", "[query redacted]", detail)
        raise TranscriptError(f"{label} failed: {detail[:240]}")
    return result


def _yt_dlp_base() -> list[str]:
    return [
        "yt-dlp",
        "--ignore-config",
        "--no-playlist",
        "--socket-timeout",
        "15",
        "--retries",
        "2",
        "--fragment-retries",
        "2",
    ]


def _safe_video_id(info: dict) -> str:
    video_id = str(info.get("id", ""))
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise TranscriptError("yt-dlp did not return a safe video ID")
    return video_id


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as audio:
            if audio.getnchannels() != 1 or audio.getframerate() != 16000 or audio.getsampwidth() != 2:
                raise TranscriptError("Normalized audio is not mono 16-kHz PCM16 WAV")
            if audio.getnframes() <= 0:
                raise TranscriptError("Normalized audio contains no samples")
            return audio.getnframes() / audio.getframerate()
    except (OSError, wave.Error) as exc:
        raise TranscriptError(f"Normalized audio is not a valid WAV file: {exc}") from exc


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


def _hardlink(source: Path, destination: Path) -> None:
    os.link(source, destination, follow_symlinks=False)


def _restore_quarantined(quarantine: Path, destination: Path) -> bool:
    try:
        _hardlink(quarantine, destination)
    except OSError:
        return False
    quarantine.unlink(missing_ok=True)
    return True


def _remove_owned(
    destination: Path, owned_identity: tuple[int, int], owned_hash: str
) -> list[Path]:
    """Remove our output, preserving an external replacement through quarantine."""
    if _identity(destination) != owned_identity or destination.is_symlink() or _sha256(destination) != owned_hash:
        return []
    quarantine = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.rollback"
    try:
        os.replace(destination, quarantine)
    except OSError:
        if _identity(quarantine) is None:
            return []
    if (
        _identity(quarantine) == owned_identity
        and not quarantine.is_symlink()
        and _sha256(quarantine) == owned_hash
    ):
        quarantine.unlink(missing_ok=True)
        return []
    return [] if _restore_quarantined(quarantine, destination) else [quarantine]


def _restore_backup(
    destination: Path,
    backup: Path,
    original_identity: tuple[int, int],
    original_hash: str,
    owned_identity: tuple[int, int],
    owned_hash: str,
) -> list[Path]:
    """Restore only over our own publication; preserve unexpected replacements."""
    current_identity = _identity(destination)
    current_hash = _sha256(destination) if current_identity and not destination.is_symlink() else None
    if current_identity == original_identity and current_hash == original_hash:
        backup.unlink(missing_ok=True)
        return []

    if current_identity is None:
        try:
            _hardlink(backup, destination)
        except OSError:
            return [backup]
        backup.unlink(missing_ok=True)
        return []

    quarantine = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.rollback"
    try:
        os.replace(destination, quarantine)
    except OSError:
        if _identity(quarantine) is None:
            return [backup] if _identity(destination) != original_identity else []

    quarantined_identity = _identity(quarantine)
    quarantined_hash = (
        _sha256(quarantine) if quarantined_identity and not quarantine.is_symlink() else None
    )
    if quarantined_identity == owned_identity and quarantined_hash == owned_hash:
        try:
            _hardlink(backup, destination)
        except OSError:
            return [backup, quarantine]
        backup.unlink(missing_ok=True)
        quarantine.unlink(missing_ok=True)
        return []

    restored = _restore_quarantined(quarantine, destination)
    recovery = [backup]
    if not restored:
        recovery.append(quarantine)
    return recovery


def publish_outputs(
    staged_vtt: Path,
    staged_json: Path,
    final_vtt: Path,
    final_json: Path,
    overwrite: bool,
) -> None:
    """Serialize same-video publishers, then publish the VTT/manifest pair."""
    lock_directory = final_vtt.parent / ".youtube-transcript-locks"
    lock_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = lock_directory / f"{final_vtt.stem}.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _publish_outputs_locked(staged_vtt, staged_json, final_vtt, final_json, overwrite)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _publish_outputs_locked(
    staged_vtt: Path,
    staged_json: Path,
    final_vtt: Path,
    final_json: Path,
    overwrite: bool,
) -> None:
    """Publish a VTT/manifest pair while holding its per-video lock."""
    destinations = (final_vtt, final_json)
    sources = (staged_vtt, staged_json)
    original_ids = {path: _identity(path) for path in destinations}
    original_hashes = {
        path: (_sha256(path) if identity and not path.is_symlink() else None)
        for path, identity in original_ids.items()
    }
    source_hashes = {path: _sha256(source) for source, path in zip(sources, destinations)}
    source_ids = {destination: _identity(source) for source, destination in zip(sources, destinations)}
    if overwrite and any(path.is_symlink() for path in destinations):
        raise TranscriptError("Refusing to overwrite symlink output paths")
    if not overwrite and any(identity is not None for identity in original_ids.values()):
        raise TranscriptError("Output already exists; refusing to overwrite it")

    backups: dict[Path, Path] = {}
    if overwrite:
        for destination, original_identity in original_ids.items():
            if original_identity is not None:
                backup = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.bak"
                try:
                    shutil.copy2(destination, backup)
                except OSError as exc:
                    for path in backups.values():
                        path.unlink(missing_ok=True)
                    raise TranscriptError(f"Could not back up existing output: {destination}") from exc
                backups[destination] = backup

    published: list[Path] = []
    try:
        if overwrite:
            for source, destination in zip(sources, destinations):
                published.append(destination)
                os.replace(source, destination)
        else:
            for source, destination in zip(sources, destinations):
                published.append(destination)
                os.link(source, destination)
            for source in sources:
                source.unlink()
    except BaseException as exc:
        recovery_paths: list[Path] = []
        for destination in published:
            backup = backups.get(destination)
            if backup:
                original_identity = original_ids[destination]
                assert original_identity is not None
                recovery_paths.extend(
                    _restore_backup(
                        destination,
                        backup,
                        original_identity,
                        original_hashes[destination],
                        source_ids[destination],
                        source_hashes[destination],
                    )
                )
            elif original_ids[destination] is None:
                recovery_paths.extend(
                    _remove_owned(destination, source_ids[destination], source_hashes[destination])
                )
        for backup in backups.values():
            if backup.exists() and backup not in recovery_paths:
                backup.unlink(missing_ok=True)
        if isinstance(exc, OSError):
            if recovery_paths:
                locations = ", ".join(str(path) for path in recovery_paths)
                raise TranscriptError(
                    f"Could not publish transcript outputs. Recovery files were preserved: {locations}"
                ) from exc
            raise TranscriptError("Could not publish transcript outputs; previous files were restored") from exc
        if recovery_paths:
            print(
                "Interrupted; recovery files were preserved: " + ", ".join(map(str, recovery_paths)),
                file=sys.stderr,
            )
        raise
    else:
        for backup in backups.values():
            backup.unlink(missing_ok=True)


def _download_caption(url: str, track_kind: str, language: str, directory: Path) -> Path:
    output = str(directory / "%(id)s.%(language)s.%(ext)s")
    command = _yt_dlp_base() + [
        "--skip-download",
        "--sub-langs",
        language,
        "--sub-format",
        "vtt/best",
        "--convert-subs",
        "vtt",
        "-o",
        output,
    ]
    command.append("--write-subs" if track_kind == "manual" else "--write-auto-subs")
    command.extend(["--", url])
    _run(command, "YouTube caption download")
    files = list(directory.glob("*.vtt"))
    if len(files) != 1:
        raise TranscriptError(f"Expected one VTT caption file, found {len(files)}")
    return files[0]


def _download_and_transcribe(
    url: str, language: str, model: Path, directory: Path, duration: float
) -> tuple[Path, float]:
    audio_template = str(directory / "source.%(ext)s")
    _run(
        _yt_dlp_base()
        + ["-f", "bestaudio/best", "-o", audio_template, "--", url],
        "YouTube audio download",
    )
    audio_files = [p for p in directory.glob("source.*") if p.is_file()]
    if len(audio_files) != 1 or audio_files[0].stat().st_size == 0:
        raise TranscriptError("Audio download produced no usable source file")

    wav_path = directory / "audio-16k-mono.wav"
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(audio_files[0]),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ],
        "Audio normalization",
    )
    if not wav_path.is_file() or wav_path.stat().st_size <= 44:
        raise TranscriptError("Audio normalization produced an empty WAV file")
    audio_duration = _wav_duration(wav_path)
    duration_tolerance = max(5.0, min(15.0, duration * 0.001))
    if abs(audio_duration - duration) > duration_tolerance:
        raise TranscriptError(
            f"Normalized audio duration ({audio_duration:.1f}s) differs from video duration "
            f"({duration:.1f}s); refusing to transcribe a partial download"
        )

    output_base = directory / "local-asr"
    _run(
        [
            "whisper-cli",
            "-m",
            str(model),
            "-l",
            language,
            "-ovtt",
            "-of",
            str(output_base),
            str(wav_path),
        ],
        "Local Whisper transcription",
    )
    vtt_path = output_base.with_suffix(".vtt")
    validate_vtt(vtt_path, duration)
    return vtt_path, audio_duration


def _tool_version(program: str) -> str | None:
    try:
        result = subprocess.run([program, "--version"], text=True, capture_output=True, check=False)
    except OSError:
        return None
    lines = (result.stdout or result.stderr).strip().splitlines()
    return lines[0][:120] if result.returncode == 0 and lines else None


def transcribe(args: argparse.Namespace) -> tuple[Path, Path, dict]:
    validate_youtube_url(args.url)
    info_result = _run(_yt_dlp_base() + ["--skip-download", "--dump-single-json", "--", args.url], "YouTube metadata lookup")
    try:
        info = json.loads(info_result.stdout)
    except json.JSONDecodeError as exc:
        raise TranscriptError("yt-dlp returned invalid video metadata") from exc
    if not isinstance(info, dict):
        raise TranscriptError("yt-dlp returned invalid video metadata")
    if info.get("extractor_key", "Youtube") not in {"Youtube", "YoutubeTab"}:
        raise TranscriptError("yt-dlp resolved this URL to a non-YouTube extractor")

    video_id = _safe_video_id(info)
    duration = video_duration_seconds(info)

    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    vtt_path = output_dir / f"{video_id}.vtt"
    json_path = output_dir / f"{video_id}.transcript.json"
    existing = [path for path in (vtt_path, json_path) if path.exists()]
    if existing and not args.force:
        raise TranscriptError(
            "Output already exists; refusing to mix or overwrite it. Use --force to replace "
            "only this video's VTT and metadata."
        )

    selected = None if args.force_asr else select_track(info, args.language)
    model = None
    if selected is None:
        model = resolve_model(args.model)

    stage = Path(tempfile.mkdtemp(prefix=f".{video_id}-", dir=output_dir))
    try:
        if selected:
            kind, language = selected
            print(f"Using YouTube {kind} captions ({language})", flush=True)
            raw_vtt = _download_caption(args.url, kind, language, stage)
            cue_count, first, last = validate_vtt(raw_vtt, float(duration))
            source = "youtube_manual" if kind == "manual" else "youtube_auto"
            model_name = None
            audio_duration = None
        else:
            print("No matching captions; running local ASR", flush=True)
            assert model is not None
            raw_vtt, audio_duration = _download_and_transcribe(
                args.url, _language_root(args.language), model, stage, float(duration)
            )
            cue_count, first, last = validate_vtt(raw_vtt, float(duration))
            source = "local_asr"
            language = _language_root(args.language)
            model_name = model.name

        tail_gap = max(0.0, float(duration) - last)
        coverage_warning = None
        if tail_gap > max(30.0, float(duration) * 0.01):
            coverage_warning = (
                f"Last subtitle cue is {tail_gap:.1f}s before video end; this may be trailing "
                "silence or incomplete coverage. Review the video tail."
            )

        metadata = {
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": info.get("title"),
            "duration_seconds": float(duration),
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "requested_language": args.language,
            "selected_language": language,
            "caption_track": selected[1] if selected else None,
            "model": model_name,
            "normalized_audio_duration_seconds": audio_duration,
            "cue_count": cue_count,
            "first_cue_seconds": first,
            "last_cue_seconds": last,
            "tail_gap_seconds": round(tail_gap, 3),
            "coverage_warning": coverage_warning,
            "vtt_sha256": _sha256(raw_vtt),
            "yt_dlp_version": _tool_version("yt-dlp"),
            "whisper_cli_version": _tool_version("whisper-cli") if source == "local_asr" else None,
        }

        staged_vtt = stage / f"{video_id}.vtt"
        staged_json = stage / f"{video_id}.transcript.json"
        shutil.copyfile(raw_vtt, staged_vtt)
        staged_json.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Publish both complete artifacts only after every validation succeeds.
        publish_outputs(staged_vtt, staged_json, vtt_path, json_path, overwrite=args.force)
        return vtt_path, json_path, metadata
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch YouTube captions or transcribe locally with whisper.cpp.")
    parser.add_argument("url", help="YouTube video URL (archived videos only)")
    parser.add_argument("-l", "--language", default="ru", help="spoken language code (default: ru)")
    parser.add_argument("-o", "--output", default=".", help="output directory (default: current directory)")
    parser.add_argument("--model", help="existing multilingual whisper.cpp model; no model is downloaded")
    parser.add_argument("--force-asr", action="store_true", help="run local ASR even when captions exist")
    parser.add_argument("--force", action="store_true", help="replace this video's existing VTT and metadata")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,20}", args.language):
        print("Error: language must be a short language tag such as ru, en, or ru-RU", file=sys.stderr)
        return 2
    try:
        vtt_path, json_path, metadata = transcribe(args)
    except TranscriptError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"VTT: {vtt_path}")
    print(f"Metadata: {json_path}")
    print(f"Source: {metadata['source']}; cues: {metadata['cue_count']}; last cue: {metadata['last_cue_seconds']:.3f}s")
    if metadata["coverage_warning"]:
        print(f"Review: {metadata['coverage_warning']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
