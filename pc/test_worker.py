"""Behavior tests for the PC worker. No GPU and no Telegram account."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import worker

TOKEN = "t" * 32
BOT = "bot-token-secret"
CHAT = "424242"
FILE_ID = "11111111-1111-4111-8111-111111111111"
FILE_ID_2 = "22222222-2222-4222-8222-222222222222"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"x" * 32


class FakeInbox:
    def __init__(self) -> None:
        self.files = {FILE_ID: MP4}
        self.messages: list[str] = []
        self.documents: list[bytes] = []
        self.downloads = 0
        self.deletes = 0
        self.extra: list[dict] = []
        self.telegram_failures_left = 0
        self.fail_all_telegram = False
        self.delete_failures_left = 0
        self.seen_auth: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.telegram.org":
            return self._telegram(request)
        auth = request.headers.get("authorization", "")
        self.seen_auth.append(f"{request.method} {request.url.path} {auth}")
        if auth != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"detail": "Unauthorized"})
        path = request.url.path
        if request.method == "GET" and path == "/files":
            items = [
                {"id": file_id, "size": len(data), "mtime": index}
                for index, (file_id, data) in enumerate(self.files.items())
            ]
            return httpx.Response(200, json=items + self.extra)
        prefix = "/files/"
        if path.startswith(prefix):
            file_id = path[len(prefix) :]
            if request.method == "GET":
                if file_id not in self.files:
                    return httpx.Response(404)
                self.downloads += 1
                return httpx.Response(200, content=self.files[file_id])
            if request.method == "DELETE":
                self.deletes += 1
                if self.delete_failures_left:
                    self.delete_failures_left -= 1
                    return httpx.Response(500, text="nope")
                if file_id not in self.files:
                    return httpx.Response(404)
                del self.files[file_id]
                return httpx.Response(204)
        return httpx.Response(404)

    def _telegram(self, request: httpx.Request) -> httpx.Response:
        if self.bot_missing(request):
            return httpx.Response(404, json={"ok": False})
        if self.fail_all_telegram or self.telegram_failures_left:
            if self.telegram_failures_left:
                self.telegram_failures_left -= 1
            return httpx.Response(500, json={"ok": False, "description": "nope"})
        if request.url.path.endswith("/sendMessage"):
            payload = json.loads(request.content.decode("utf-8"))
            if payload.get("chat_id") != CHAT:
                return httpx.Response(400, json={"ok": False, "description": "chat"})
            self.messages.append(payload["text"])
            return httpx.Response(200, json={"ok": True, "result": {}})
        if request.url.path.endswith("/sendDocument"):
            self.documents.append(bytes(request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(404, json={"ok": False})

    def bot_missing(self, request: httpx.Request) -> bool:
        return f"/bot{BOT}/" not in request.url.path


class WorkerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._env = os.environ.copy()
        self.inbox = FakeInbox()
        self.client = httpx.Client(
            transport=httpx.MockTransport(self.inbox.handler),
            follow_redirects=False,
            timeout=30.0,
        )
        self.cfg = worker.Config(
            server_url="http://inbox.test:9",
            token=TOKEN,
            tg_bot_token=BOT,
            tg_chat_id=CHAT,
            poll_seconds=10,
            model="large-v3",
        )
        self.state = worker.State()
        self.transcribe_calls: list[str] = []
        self.temp_dirs: list[str] = []

    def tearDown(self) -> None:
        self.client.close()
        for handler in list(worker.log.handlers):
            handler.close()
            worker.log.removeHandler(handler)
        worker.log.addHandler(worker.logging.NullHandler())
        os.environ.clear()
        os.environ.update(self._env)

    def _poll(self, transcript: str = "hello", fail_ids: set[str] | None = None) -> None:
        failed = fail_ids or set()
        real_mkdtemp = tempfile.mkdtemp

        def track(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            self.temp_dirs.append(path)
            return path

        def extract(ffmpeg, src, dest):
            self.assertTrue(src.is_file())
            self.assertEqual(src.read_bytes(), MP4)
            dest.write_bytes(b"RIFFwav")

        def transcribe(model, wav):
            self.transcribe_calls.append(wav.stem)
            if wav.stem in failed:
                raise RuntimeError("bad audio")
            return transcript

        with (
            patch("worker.tempfile.mkdtemp", track),
            patch("worker.extract_wav", extract),
            patch("worker.transcribe_wav", transcribe),
        ):
            worker.poll_once(self.client, self.cfg, object(), "ffmpeg", self.state)
        for path in self.temp_dirs:
            self.assertFalse(Path(path).exists(), path)

    def test_config_requires_env_file(self) -> None:
        missing = Path(tempfile.gettempdir()) / "transcribe-missing-env-file.env"
        with self.assertRaises(SystemExit):
            worker.load_config(missing)

    def test_config_rejects_short_token_and_bad_poll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "SERVER_URL=http://203.0.113.10:8080\nTOKEN=short\n"
                "TG_BOT_TOKEN=abc\nTG_CHAT_ID=1\n",
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                worker.load_config(path)
            path.write_text(
                "SERVER_URL=http://203.0.113.10:8080\n"
                f"TOKEN={TOKEN}\nTG_BOT_TOKEN=abc\nTG_CHAT_ID=1\nPOLL_SECONDS=0\n",
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                worker.load_config(path)

    def test_config_defaults_and_strips_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_bytes(
                (
                    "\ufeffSERVER_URL=http://203.0.113.10:8080/\n"
                    f"TOKEN={TOKEN}\nTG_BOT_TOKEN=abc\nTG_CHAT_ID=99\n"
                ).encode("utf-8")
            )
            cfg = worker.load_config(path)
        self.assertEqual(cfg.server_url, "http://203.0.113.10:8080")
        self.assertEqual(cfg.poll_seconds, 10)
        self.assertEqual(cfg.model, "large-v3")
        self.assertEqual(cfg.tg_chat_id, "99")

    def test_transcribe_is_plain_text_without_a_language_hint(self) -> None:
        class Info:
            language = "ru"
            language_probability = 0.91

        class Segment:
            def __init__(self, text: str) -> None:
                self.text = text

        class Model:
            def transcribe(self, path, vad_filter=False):
                self.path = path
                self.vad_filter = vad_filter
                return iter(
                    [Segment(" hello"), Segment("   "), Segment("мир")]
                ), Info()

        model = Model()
        text = worker.transcribe_wav(model, Path("clip.wav"))
        self.assertEqual(text, "hello\nмир")
        self.assertTrue(model.vad_filter)
        self.assertEqual(model.path, "clip.wav")

    def test_telegram_routing(self) -> None:
        worker.send_transcript(self.client, self.cfg, FILE_ID, "  ")
        worker.send_transcript(self.client, self.cfg, FILE_ID, "a" * 4096)
        worker.send_transcript(self.client, self.cfg, FILE_ID, "a" * 4097)
        worker.send_transcript(self.client, self.cfg, FILE_ID, ("a" * 4095) + "😀")
        self.assertEqual(self.inbox.messages[0], "no speech found")
        self.assertEqual(self.inbox.messages[1], "a" * 4096)
        self.assertEqual(len(self.inbox.documents), 2)
        self.assertIn(b"a" * 4097, self.inbox.documents[0])
        self.assertIn(FILE_ID.encode("ascii") + b".txt", self.inbox.documents[0])
        self.assertIn("😀".encode("utf-8"), self.inbox.documents[1])

    def test_success_deletes_only_after_telegram(self) -> None:
        self._poll("привет")
        self.assertEqual(self.inbox.messages, ["привет"])
        self.assertEqual(self.inbox.downloads, 1)
        self.assertEqual(self.inbox.deletes, 1)
        self.assertNotIn(FILE_ID, self.inbox.files)
        self.assertEqual(self.transcribe_calls, [FILE_ID])
        self.assertTrue(self.inbox.seen_auth)
        self.assertTrue(
            all(item.endswith(f"Bearer {TOKEN}") for item in self.inbox.seen_auth)
        )

    def test_telegram_failure_keeps_the_file(self) -> None:
        self.inbox.telegram_failures_left = 1
        self._poll("hello")
        self.assertEqual(self.inbox.messages, [])
        self.assertIn(FILE_ID, self.inbox.files)
        self.assertEqual(self.inbox.deletes, 0)
        self.assertEqual(self.state.failures[FILE_ID], 1)
        self.assertNotIn(BOT, self.state.reasons[FILE_ID])
        self.assertNotIn(TOKEN, self.state.reasons[FILE_ID])

    def test_delete_failure_does_not_resend(self) -> None:
        self.inbox.delete_failures_left = 1
        self._poll("hello")
        self.assertEqual(self.inbox.messages, ["hello"])
        self.assertIn(FILE_ID, self.inbox.files)
        self.assertIn(FILE_ID, self.state.pending_delete)
        self._poll("hello")
        self.assertEqual(self.inbox.messages, ["hello"])
        self.assertEqual(self.transcribe_calls, [FILE_ID])
        self.assertNotIn(FILE_ID, self.inbox.files)
        self.assertNotIn(FILE_ID, self.state.pending_delete)

    def test_three_failures_notify_and_skip_until_restart(self) -> None:
        self.inbox.telegram_failures_left = 3
        for _ in range(3):
            self._poll("hello")
        self.assertEqual(self.inbox.downloads, 3)
        self.assertEqual(self.state.failures[FILE_ID], 3)
        self.assertIn(FILE_ID, self.state.skipped)
        self.assertEqual(len(self.inbox.messages), 1)
        self.assertTrue(self.inbox.messages[0].startswith("Failed 3 times"))
        self.assertIn(FILE_ID, self.inbox.messages[0])
        self.assertIn(FILE_ID, self.inbox.files)
        downloads = self.inbox.downloads
        self._poll("hello")
        self.assertEqual(self.inbox.downloads, downloads)
        self.assertEqual(self.transcribe_calls, [FILE_ID, FILE_ID, FILE_ID])

    def test_skip_waits_until_the_notice_is_delivered(self) -> None:
        self.inbox.fail_all_telegram = True
        for _ in range(4):
            self._poll("hello")
        self.assertNotIn(FILE_ID, self.state.skipped)
        self.assertEqual(self.inbox.downloads, 3)
        self.inbox.fail_all_telegram = False
        self._poll("hello")
        self.assertIn(FILE_ID, self.state.skipped)
        self.assertEqual(self.inbox.downloads, 3)
        self.assertEqual(len(self.inbox.messages), 1)

    def test_empty_transcript_is_a_successful_delivery(self) -> None:
        self._poll("   ")
        self.assertEqual(self.inbox.messages, ["no speech found"])
        self.assertNotIn(FILE_ID, self.inbox.files)

    def test_long_transcript_is_a_document_and_then_deleted(self) -> None:
        self._poll("a" * 5000)
        self.assertEqual(self.inbox.messages, [])
        self.assertEqual(len(self.inbox.documents), 1)
        self.assertIn(b"a" * 5000, self.inbox.documents[0])
        self.assertNotIn(FILE_ID, self.inbox.files)

    def test_one_failure_does_not_block_the_next_file(self) -> None:
        self.inbox.files[FILE_ID_2] = MP4
        self._poll("ok", fail_ids={FILE_ID})
        self.assertEqual(self.state.failures[FILE_ID], 1)
        self.assertIn(FILE_ID, self.inbox.files)
        self.assertNotIn(FILE_ID_2, self.inbox.files)
        self.assertEqual(self.inbox.messages, ["ok"])

    def test_invalid_id_is_not_downloaded(self) -> None:
        self.inbox.files.clear()
        self.inbox.extra = [
            {"id": "../etc/passwd", "size": 1, "mtime": 0},
            {"id": "NOT-A-UUID", "size": 1, "mtime": 0},
        ]
        self._poll()
        self._poll()
        self.assertEqual(self.inbox.downloads, 0)
        self.assertEqual(len(self.state.ignored), 2)

    def test_unauthorized_list_raises_and_downloads_nothing(self) -> None:
        cfg = replace(self.cfg, token="u" * 32)
        with self.assertRaises(httpx.HTTPStatusError):
            worker.poll_once(self.client, cfg, object(), "ffmpeg", self.state)
        self.assertEqual(self.inbox.downloads, 0)

    def test_log_file_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "worker.log"
            worker.setup_logging(path)
            try:
                worker.log.error(
                    "%s",
                    worker.redact(f"leaked {TOKEN} and {BOT} url", self.cfg),
                )
                for handler in worker.log.handlers:
                    handler.flush()
                text = path.read_text(encoding="utf-8")
            finally:
                for handler in list(worker.log.handlers):
                    handler.close()
                    worker.log.removeHandler(handler)
        self.assertIn("leaked", text)
        self.assertIn("<redacted>", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(BOT, text)

    def test_ffmpeg_mono_16k(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        self.assertIsNotNone(ffmpeg)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "in.mp4"
            wav_path = root / "out.wav"
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=16x16:d=1",
                    "-shortest",
                    str(src),
                ],
                check=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
            )
            worker.extract_wav(ffmpeg, src, wav_path)
            with wave.open(str(wav_path), "rb") as handle:
                self.assertEqual(handle.getnchannels(), 1)
                self.assertEqual(handle.getframerate(), 16000)
                self.assertGreater(handle.getnframes(), 0)

    def test_ffmpeg_rejects_non_media(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        self.assertIsNotNone(ffmpeg)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "bad.mp4"
            src.write_bytes(b"not a video")
            dest = Path(tmp) / "out.wav"
            with self.assertRaises(RuntimeError) as caught:
                worker.extract_wav(ffmpeg, src, dest)
        self.assertIn("ffmpeg exited", str(caught.exception))

    def test_telegram_url_contains_bot_token(self) -> None:
        url = worker.telegram_url(self.cfg, "sendMessage")
        self.assertEqual(urlsplit(url).path, f"/bot{BOT}/sendMessage")


if __name__ == "__main__":
    unittest.main()
