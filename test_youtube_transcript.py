import json
import tempfile
import threading
import time
import unittest
import wave
from argparse import Namespace
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from youtube_transcript import (
    TranscriptError,
    build_parser,
    publish_outputs,
    resolve_model,
    select_track,
    transcribe,
    validate_youtube_url,
    validate_vtt,
    video_duration_seconds,
)


class SelectTrackTests(unittest.TestCase):
    def test_manual_track_precedes_automatic_track(self):
        info = {
            "subtitles": {"ru": [{"ext": "vtt"}]},
            "automatic_captions": {"ru-orig": [{"ext": "vtt"}]},
        }
        self.assertEqual(select_track(info, "ru"), ("manual", "ru"))

    def test_original_auto_track_precedes_other_language_tracks(self):
        info = {
            "subtitles": {},
            "automatic_captions": {
                "ru": [{"ext": "vtt"}],
                "ru-orig": [{"ext": "vtt"}],
                "en": [{"ext": "vtt"}],
            },
        }
        self.assertEqual(select_track(info, "ru"), ("automatic", "ru-orig"))

    def test_live_chat_is_not_a_speech_transcript(self):
        info = {"subtitles": {"live_chat": [{"ext": "json3"}]}}
        self.assertIsNone(select_track(info, "ru"))

    def test_no_matching_language_returns_no_track(self):
        info = {"subtitles": {"en": [{"ext": "vtt"}]}, "automatic_captions": {}}
        self.assertIsNone(select_track(info, "ru"))

    def test_exact_auto_track_requires_matching_video_language(self):
        info = {"language": "en", "automatic_captions": {"ru": [{"ext": "vtt"}]}}
        self.assertIsNone(select_track(info, "ru"))

    def test_region_requested_language_matches_original_track(self):
        info = {"language": "ru", "automatic_captions": {"ru-orig": [{"ext": "vtt"}]}}
        self.assertEqual(select_track(info, "ru-RU"), ("automatic", "ru-orig"))

    def test_locale_case_and_separator_match_manual_track(self):
        info = {"subtitles": {"pt-BR": [{"ext": "vtt"}]}}
        self.assertEqual(select_track(info, "pt_br"), ("manual", "pt-BR"))

    def test_malformed_track_metadata_is_a_clear_error(self):
        with self.assertRaisesRegex(TranscriptError, "metadata"):
            select_track({"subtitles": []}, "ru")
        with self.assertRaisesRegex(TranscriptError, "entry"):
            select_track({"subtitles": {"ru": "not-a-track-list"}}, "ru")


class VttValidationTests(unittest.TestCase):
    def write_vtt(self, text):
        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name) / "transcript.vtt"
        path.write_text(text, encoding="utf-8")
        return temp, path

    def test_accepts_timestamped_full_coverage_vtt(self):
        temp, path = self.write_vtt(
            "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nПривет.\n\n"
            "00:00:55.000 --> 00:01:00.000\nДо свидания.\n"
        )
        with temp:
            self.assertEqual(validate_vtt(path, 60), (2, 1.0, 60.0))

    def test_accepts_webvtt_timestamp_without_hour_field(self):
        temp, path = self.write_vtt(
            "WEBVTT\n\n00:01.000 --> 00:04.000\nКороткий клип.\n"
        )
        with temp:
            self.assertEqual(validate_vtt(path, 10), (1, 1.0, 4.0))

    def test_rejects_empty_or_malformed_vtt(self):
        for content in (
            "",
            "WEBVTT\n\nнет временных отметок\n",
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<c></c>\n",
            "WEBVTTbroken\n\n00:00:01.000 --> 00:00:02.000\nТекст.\n",
            "WEBVTT\n\n00:00:01.000 --> 00:00:99.000\nТекст.\n",
            "WEBVTT\n\nNOTE fake cue\n00:00:59.000 --> 00:01:00.000\n",
            "WEBVTT\n\n00:00:01.000 -> 00:00:02.000\nТекст.\n",
        ):
            temp, path = self.write_vtt(content)
            with temp, self.assertRaises(TranscriptError):
                validate_vtt(path, 60)

    def test_empty_final_cue_does_not_extend_text_coverage(self):
        temp, path = self.write_vtt(
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nТекст.\n\n"
            "00:00:59.000 --> 00:01:00.000\n\n"
        )
        with temp:
            self.assertEqual(validate_vtt(path, 60), (1, 1.0, 2.0))

    def test_rejects_cues_past_video_duration(self):
        temp, path = self.write_vtt(
            "WEBVTT\n\n00:01:40.000 --> 00:01:44.000\nЗа концом видео.\n"
        )
        with temp, self.assertRaises(TranscriptError):
            validate_vtt(path, 60)


class VideoMetadataTests(unittest.TestCase):
    def test_rejects_active_live_or_nonfinite_duration(self):
        for info in (
            {"duration": 60, "live_status": "is_live"},
            {"duration": 60, "is_live": True},
            {"duration": float("nan"), "live_status": "was_live"},
            {"duration": float("inf"), "live_status": "not_live"},
            {"duration": True, "live_status": "not_live"},
            {"duration": 10**10000, "live_status": "not_live"},
        ):
            with self.subTest(info=info), self.assertRaises(TranscriptError):
                video_duration_seconds(info)

    def test_accepts_archived_video_with_finite_duration(self):
        self.assertEqual(video_duration_seconds({"duration": 60, "live_status": "was_live"}), 60.0)

    def test_rejects_non_youtube_urls(self):
        with self.assertRaisesRegex(TranscriptError, "YouTube URL"):
            validate_youtube_url("https://vimeo.com/12345678")


class ModelTests(unittest.TestCase):
    def test_requires_existing_multilingual_model(self):
        with tempfile.TemporaryDirectory() as folder:
            english_only = Path(folder) / "ggml-small.en.bin"
            english_only.write_bytes(b"model")
            with self.assertRaises(TranscriptError):
                resolve_model(str(english_only))

            multilingual = Path(folder) / "ggml-small.bin"
            multilingual.write_bytes(b"model")
            self.assertEqual(resolve_model(str(multilingual)), multilingual.resolve())


class CliCopyTests(unittest.TestCase):
    def test_force_asr_is_described_as_separate_local_transcription(self):
        help_text = build_parser().format_help()
        self.assertIn("even when captions exist", help_text)
        self.assertNotIn("for comparison", help_text)


class PublishTests(unittest.TestCase):
    def test_failed_forced_pair_publish_restores_both_previous_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            staged_vtt = root / "new.vtt"
            staged_json = root / "new.json"
            final_vtt = root / "video.vtt"
            final_json = root / "video.json"
            staged_vtt.write_text("new captions", encoding="utf-8")
            staged_json.write_text("new metadata", encoding="utf-8")
            final_vtt.write_text("old captions", encoding="utf-8")
            final_json.write_text("old metadata", encoding="utf-8")

            import os

            real_replace = os.replace
            calls = 0

            def fail_second_replace(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated storage failure")
                return real_replace(source, destination)

            with patch("youtube_transcript.os.replace", side_effect=fail_second_replace):
                with self.assertRaisesRegex(TranscriptError, "publish"):
                    publish_outputs(staged_vtt, staged_json, final_vtt, final_json, overwrite=True)

            self.assertEqual(final_vtt.read_text(encoding="utf-8"), "old captions")
            self.assertEqual(final_json.read_text(encoding="utf-8"), "old metadata")

    def test_interrupted_forced_pair_publish_restores_previous_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            staged_vtt, staged_json = root / "new.vtt", root / "new.json"
            final_vtt, final_json = root / "video.vtt", root / "video.json"
            staged_vtt.write_text("new captions", encoding="utf-8")
            staged_json.write_text("new metadata", encoding="utf-8")
            final_vtt.write_text("old captions", encoding="utf-8")
            final_json.write_text("old metadata", encoding="utf-8")

            import os

            real_replace = os.replace
            calls = 0

            def interrupt_second_replace(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise KeyboardInterrupt
                return real_replace(source, destination)

            with patch("youtube_transcript.os.replace", side_effect=interrupt_second_replace):
                with self.assertRaises(KeyboardInterrupt):
                    publish_outputs(staged_vtt, staged_json, final_vtt, final_json, overwrite=True)
            self.assertEqual(final_vtt.read_text(encoding="utf-8"), "old captions")
            self.assertEqual(final_json.read_text(encoding="utf-8"), "old metadata")

    def test_interrupt_after_replace_syscall_restores_previous_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            staged_vtt, staged_json = root / "new.vtt", root / "new.json"
            final_vtt, final_json = root / "video.vtt", root / "video.json"
            staged_vtt.write_text("new captions", encoding="utf-8")
            staged_json.write_text("new metadata", encoding="utf-8")
            final_vtt.write_text("old captions", encoding="utf-8")
            final_json.write_text("old metadata", encoding="utf-8")

            import os

            real_replace = os.replace
            calls = 0

            def interrupt_after_first_replace(source, destination):
                nonlocal calls
                calls += 1
                result = real_replace(source, destination)
                if calls == 1:
                    raise KeyboardInterrupt
                return result

            with patch("youtube_transcript.os.replace", side_effect=interrupt_after_first_replace):
                with self.assertRaises(KeyboardInterrupt):
                    publish_outputs(staged_vtt, staged_json, final_vtt, final_json, overwrite=True)
            self.assertEqual(final_vtt.read_text(encoding="utf-8"), "old captions")
            self.assertEqual(final_json.read_text(encoding="utf-8"), "old metadata")

    def test_link_race_does_not_remove_concurrent_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            staged_vtt, staged_json = root / "new.vtt", root / "new.json"
            final_vtt, final_json = root / "video.vtt", root / "video.json"
            staged_vtt.write_text("new captions", encoding="utf-8")
            staged_json.write_text("new metadata", encoding="utf-8")

            import os

            real_link = os.link
            calls = 0

            def race_link(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    Path(destination).write_text("other process", encoding="utf-8")
                return real_link(source, destination)

            with patch("youtube_transcript.os.link", side_effect=race_link):
                with self.assertRaisesRegex(TranscriptError, "publish"):
                    publish_outputs(staged_vtt, staged_json, final_vtt, final_json, overwrite=False)
            self.assertFalse(final_vtt.exists())
            self.assertEqual(final_json.read_text(encoding="utf-8"), "other process")

    def test_forced_rollback_does_not_follow_replaced_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            staged_vtt, staged_json = root / "new.vtt", root / "new.json"
            final_vtt, final_json = root / "video.vtt", root / "video.json"
            unrelated = root / "unrelated.txt"
            staged_vtt.write_text("new captions", encoding="utf-8")
            staged_json.write_text("new metadata", encoding="utf-8")
            final_vtt.write_text("old captions", encoding="utf-8")
            final_json.write_text("old metadata", encoding="utf-8")
            unrelated.write_text("unrelated data", encoding="utf-8")

            import os

            real_replace = os.replace
            calls = 0

            def race_replace(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    final_vtt.unlink()
                    final_vtt.symlink_to(unrelated)
                    raise OSError("simulated second replacement failure")
                return real_replace(source, destination)

            with patch("youtube_transcript.os.replace", side_effect=race_replace):
                with self.assertRaises(TranscriptError):
                    publish_outputs(staged_vtt, staged_json, final_vtt, final_json, overwrite=True)
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "unrelated data")
            self.assertTrue(final_vtt.is_symlink())

    def test_forced_rollback_preserves_concurrent_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            staged_vtt, staged_json = root / "new.vtt", root / "new.json"
            final_vtt, final_json = root / "video.vtt", root / "video.json"
            staged_vtt.write_text("new captions", encoding="utf-8")
            staged_json.write_text("new metadata", encoding="utf-8")
            final_vtt.write_text("old captions", encoding="utf-8")
            final_json.write_text("old metadata", encoding="utf-8")

            import os

            real_replace = os.replace
            calls = 0

            def edit_before_failed_replace(source, destination):
                nonlocal calls
                calls += 1
                if calls == 1:
                    final_vtt.unlink()
                    final_vtt.write_text("concurrent edit", encoding="utf-8")
                    raise OSError("simulated replacement failure")
                return real_replace(source, destination)

            with patch("youtube_transcript.os.replace", side_effect=edit_before_failed_replace):
                with self.assertRaisesRegex(TranscriptError, "Recovery files were preserved"):
                    publish_outputs(staged_vtt, staged_json, final_vtt, final_json, overwrite=True)
            self.assertEqual(final_vtt.read_text(encoding="utf-8"), "concurrent edit")
            self.assertEqual(final_json.read_text(encoding="utf-8"), "old metadata")
            recovery = list(root.glob(".video.vtt.*.bak"))
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0].read_text(encoding="utf-8"), "old captions")

    def test_forced_rollback_preserves_in_place_concurrent_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            staged_vtt, staged_json = root / "new.vtt", root / "new.json"
            final_vtt, final_json = root / "video.vtt", root / "video.json"
            staged_vtt.write_text("new captions", encoding="utf-8")
            staged_json.write_text("new metadata", encoding="utf-8")
            final_vtt.write_text("old captions", encoding="utf-8")
            final_json.write_text("old metadata", encoding="utf-8")
            import os

            real_replace = os.replace
            calls = 0

            def edit_published_file(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    nonlocal_published_inode[0] = final_vtt.stat().st_ino
                    final_vtt.write_text("concurrent in-place edit", encoding="utf-8")
                    raise OSError("simulated second replacement failure")
                return real_replace(source, destination)

            nonlocal_published_inode = [None]
            with patch("youtube_transcript.os.replace", side_effect=edit_published_file):
                with self.assertRaisesRegex(TranscriptError, "Recovery files were preserved"):
                    publish_outputs(staged_vtt, staged_json, final_vtt, final_json, overwrite=True)
            self.assertEqual(final_vtt.stat().st_ino, nonlocal_published_inode[0])
            self.assertEqual(final_vtt.read_text(encoding="utf-8"), "concurrent in-place edit")
            self.assertEqual(len(list(root.glob(".video.vtt.*.bak"))), 1)

    def test_no_force_rollback_preserves_replacement_after_identity_check(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            staged_vtt, staged_json = root / "new.vtt", root / "new.json"
            final_vtt, final_json = root / "video.vtt", root / "video.json"
            staged_vtt.write_text("new captions", encoding="utf-8")
            staged_json.write_text("new metadata", encoding="utf-8")

            import os

            real_link = os.link
            real_replace = os.replace
            calls = 0

            def fail_second_link(source, destination, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated second link failure")
                return real_link(source, destination, **kwargs)

            def replace_first_link_after_identity_check(source, destination):
                if Path(source) == final_vtt:
                    final_vtt.unlink()
                    final_vtt.write_text("concurrent replacement", encoding="utf-8")
                return real_replace(source, destination)

            with patch("youtube_transcript.os.link", side_effect=fail_second_link), patch(
                "youtube_transcript.os.replace", side_effect=replace_first_link_after_identity_check
            ):
                with self.assertRaises(TranscriptError):
                    publish_outputs(staged_vtt, staged_json, final_vtt, final_json, overwrite=False)
            self.assertEqual(final_vtt.read_text(encoding="utf-8"), "concurrent replacement")

    def test_same_video_publishers_cannot_mix_vtt_and_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            final_vtt, final_json = root / "video.vtt", root / "video.json"
            final_vtt.write_text("old vtt", encoding="utf-8")
            final_json.write_text("old json", encoding="utf-8")
            a_vtt, a_json, b_vtt, b_json = (root / name for name in ("a.vtt", "a.json", "b.vtt", "b.json"))
            a_vtt.write_text("writer A vtt", encoding="utf-8")
            a_json.write_text("writer A json", encoding="utf-8")
            b_vtt.write_text("writer B vtt", encoding="utf-8")
            b_json.write_text("writer B json", encoding="utf-8")
            first_replaced = threading.Event()
            release_first = threading.Event()
            second_done = threading.Event()
            failures = []

            import os

            real_replace = os.replace

            def pause_after_writer_a_vtt(source, destination):
                result = real_replace(source, destination)
                if Path(source) == a_vtt:
                    first_replaced.set()
                    if not release_first.wait(5):
                        raise TimeoutError("test did not release writer A")
                return result

            def writer_a():
                try:
                    publish_outputs(a_vtt, a_json, final_vtt, final_json, overwrite=True)
                except BaseException as exc:
                    failures.append(exc)

            def writer_b():
                try:
                    first_replaced.wait(5)
                    publish_outputs(b_vtt, b_json, final_vtt, final_json, overwrite=True)
                except BaseException as exc:
                    failures.append(exc)
                finally:
                    second_done.set()

            with patch("youtube_transcript.os.replace", side_effect=pause_after_writer_a_vtt):
                thread_a = threading.Thread(target=writer_a)
                thread_b = threading.Thread(target=writer_b)
                thread_a.start()
                thread_b.start()
                self.assertTrue(first_replaced.wait(2))
                time.sleep(0.05)
                self.assertFalse(second_done.is_set())
                release_first.set()
                thread_a.join(5)
                thread_b.join(5)

            self.assertFalse(thread_a.is_alive())
            self.assertFalse(thread_b.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(final_vtt.read_text(encoding="utf-8"), "writer B vtt")
            self.assertEqual(final_json.read_text(encoding="utf-8"), "writer B json")


class RoutingTests(unittest.TestCase):
    def test_no_matching_caption_routes_to_local_asr(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "ggml-small.bin"
            model.write_bytes(b"model")
            output = root / "out"
            args = Namespace(
                url="https://www.youtube.com/watch?v=abcdefghijk",
                language="ru-RU",
                output=str(output),
                force=False,
                force_asr=False,
                model=str(model),
            )
            commands = []

            def fake_run(command, label):
                commands.append(command)
                if "--dump-single-json" in command:
                    data = {"id": "abcdefghijk", "title": "sample", "duration": 60, "language": "ru"}
                    return CompletedProcess(command, 0, json.dumps(data), "")
                if command[0] == "yt-dlp":
                    template = command[command.index("-o") + 1]
                    Path(template.split("%(ext)s")[0] + "webm").write_bytes(b"audio")
                elif command[0] == "ffmpeg":
                    self.write_wav(Path(command[-1]), 60)
                elif command[0] == "whisper-cli":
                    output_base = Path(command[command.index("-of") + 1])
                    output_base.with_suffix(".vtt").write_text(
                        "WEBVTT\n\n00:00:00.000 --> 00:00:55.000\nРеальная речь.\n",
                        encoding="utf-8",
                    )
                return CompletedProcess(command, 0, "", "")

            with patch("youtube_transcript._run", side_effect=fake_run), patch(
                "youtube_transcript._tool_version", return_value="test-version"
            ):
                vtt, metadata_path, metadata = transcribe(args)

            self.assertEqual(metadata["source"], "local_asr")
            self.assertEqual(metadata["requested_language"], "ru-RU")
            self.assertEqual(metadata["selected_language"], "ru")
            self.assertTrue(vtt.is_file())
            self.assertTrue(metadata_path.is_file())
            whisper_command = next(command for command in commands if command[0] == "whisper-cli")
            self.assertIn("-l", whisper_command)
            self.assertEqual(whisper_command[whisper_command.index("-l") + 1], "ru")
            self.assertIn("-m", whisper_command)
            self.assertNotIn("-tr", whisper_command)
            self.assertTrue(all("--ignore-config" in command for command in commands if command[0] == "yt-dlp"))

    @staticmethod
    def write_wav(path, duration_seconds):
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * (16000 * duration_seconds))

    def test_truncated_audio_does_not_run_asr_or_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "ggml-small.bin"
            model.write_bytes(b"model")
            output = root / "out"
            args = Namespace(
                url="https://www.youtube.com/watch?v=abcdefghijk",
                language="ru",
                output=str(output),
                force=False,
                force_asr=False,
                model=str(model),
            )
            commands = []

            def fake_run(command, label):
                commands.append(command)
                if "--dump-single-json" in command:
                    info = {"id": "abcdefghijk", "duration": 60, "language": "ru"}
                    return CompletedProcess(command, 0, json.dumps(info), "")
                if command[0] == "yt-dlp":
                    template = command[command.index("-o") + 1]
                    Path(template.split("%(ext)s")[0] + "webm").write_bytes(b"audio")
                elif command[0] == "ffmpeg":
                    self.write_wav(Path(command[-1]), 2)
                return CompletedProcess(command, 0, "", "")

            with patch("youtube_transcript._run", side_effect=fake_run), patch(
                "youtube_transcript._tool_version", return_value="test-version"
            ):
                with self.assertRaisesRegex(TranscriptError, "audio duration"):
                    transcribe(args)

            self.assertFalse(any(command[0] == "whisper-cli" for command in commands))
            self.assertFalse(list(output.glob("*.transcript.json")))

    def test_metadata_failure_does_not_masquerade_as_missing_captions(self):
        with tempfile.TemporaryDirectory() as folder:
            args = Namespace(
                url="https://www.youtube.com/watch?v=abcdefghijk",
                language="ru",
                output=folder,
                force=False,
                force_asr=False,
                model=None,
            )
            with patch("youtube_transcript._run", side_effect=TranscriptError("blocked")) as run:
                with self.assertRaisesRegex(TranscriptError, "blocked"):
                    transcribe(args)
                self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
