"""Poll the VPS inbox, transcribe each video, and send the text to Telegram."""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.parse
import uuid
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
from dotenv import load_dotenv

log = logging.getLogger("transcribe.worker")
if not log.handlers:
    log.addHandler(logging.NullHandler())

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
TELEGRAM_TEXT_LIMIT = 4096
MAX_FAILURES = 3
NO_SPEECH = "no speech found"
FFMPEG_MISSING = (
    "ffmpeg is not on PATH. Install it, open a new terminal, and run ffmpeg -version.\n"
    "Windows: winget install Gyan.FFmpeg"
)


class FileGone(Exception):
    """The inbox no longer has this id."""


@dataclass(frozen=True)
class Config:
    server_url: str
    token: str
    tg_bot_token: str
    tg_chat_id: str
    poll_seconds: float
    model: str


@dataclass
class State:
    failures: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)
    pending_delete: set[str] = field(default_factory=set)
    ignored: set[str] = field(default_factory=set)


def die(message: str) -> None:
    if any(not isinstance(handler, logging.NullHandler) for handler in log.handlers):
        log.error("%s", message)
    else:
        print(message, file=sys.stderr)
    raise SystemExit(1)


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def clip_telegram(text: str) -> str:
    if utf16_len(text) <= TELEGRAM_TEXT_LIMIT:
        return text
    raw = text.encode("utf-16-le")[: TELEGRAM_TEXT_LIMIT * 2]
    return raw.decode("utf-16-le", errors="ignore")


def redact(text: str, cfg: Config) -> str:
    for secret in (cfg.tg_bot_token, cfg.token):
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def valid_id(file_id: object) -> bool:
    if not isinstance(file_id, str) or _UUID_RE.fullmatch(file_id) is None:
        return False
    try:
        return str(uuid.UUID(file_id)) == file_id
    except ValueError:
        return False


def require_id(file_id: str) -> None:
    if not valid_id(file_id):
        raise RuntimeError("invalid id")


def load_config(env_path: Path | None = None) -> Config:
    if env_path is None:
        env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        die(
            f"Refusing to start: {env_path} does not exist. "
            "Copy .env.example to .env and fill it in."
        )
    load_dotenv(env_path, override=True, encoding="utf-8-sig")

    server = os.environ.get("SERVER_URL", "").strip().rstrip("/")
    token = os.environ.get("TOKEN", "").strip()
    bot = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT_ID", "").strip()
    poll_raw = os.environ.get("POLL_SECONDS", "10").strip() or "10"
    model = os.environ.get("MODEL", "large-v3").strip() or "large-v3"

    if not server or "<" in server or ">" in server:
        die(
            "Refusing to start: set SERVER_URL in .env to your inbox, "
            "for example http://203.0.113.10:8080"
        )
    parsed = urllib.parse.urlsplit(server)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        die("Refusing to start: SERVER_URL must look like http://203.0.113.10:8080")
    if parsed.username or parsed.password:
        die("Refusing to start: SERVER_URL must not contain a username or password")
    if len(token) < 32:
        die(
            "Refusing to start: TOKEN must be at least 32 characters. "
            'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    if not bot:
        die("Refusing to start: TG_BOT_TOKEN is empty")
    if not chat:
        die("Refusing to start: TG_CHAT_ID is empty")
    try:
        poll = float(poll_raw)
    except ValueError:
        die("Refusing to start: POLL_SECONDS must be a number")
    if not math.isfinite(poll) or poll <= 0:
        die("Refusing to start: POLL_SECONDS must be greater than 0")
    return Config(
        server_url=server,
        token=token,
        tg_bot_token=bot,
        tg_chat_id=chat,
        poll_seconds=poll,
        model=model,
    )


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    log.propagate = False
    log.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    rotating = RotatingFileHandler(
        log_path,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    log.addHandler(console)
    log.addHandler(rotating)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        die(FFMPEG_MISSING)
    try:
        completed = subprocess.run(
            [found, "-version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        die(FFMPEG_MISSING)
    if completed.returncode != 0:
        die(FFMPEG_MISSING)
    return found


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def download_file(
    client: httpx.Client, cfg: Config, file_id: str, dest: Path
) -> None:
    require_id(file_id)
    url = f"{cfg.server_url}/files/{file_id}"
    with client.stream("GET", url, headers=bearer(cfg.token)) as response:
        if response.status_code == 404:
            raise FileGone(file_id)
        response.raise_for_status()
        expected = response.headers.get("content-length")
        if expected is None:
            expected_bytes = None
        elif expected.isdigit():
            expected_bytes = int(expected)
        else:
            raise RuntimeError("Content-Length is not a number")
        written = 0
        with dest.open("wb") as handle:
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                handle.write(chunk)
                written += len(chunk)
    if expected_bytes is not None and written != expected_bytes:
        raise RuntimeError(
            f"download size {written} does not match Content-Length {expected_bytes}"
        )
    if written == 0:
        raise RuntimeError("downloaded file is empty")


def extract_wav(ffmpeg: str, src: Path, dest: Path) -> None:
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffmpeg timed out after 600 seconds") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg exited {completed.returncode}: {detail}")
    if not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced an empty wav")


def load_model(model_name: str):
    from faster_whisper import WhisperModel

    return WhisperModel(model_name, device="cuda", compute_type="float16")


def transcribe_wav(model, wav_path: Path) -> str:
    segments, info = model.transcribe(str(wav_path), vad_filter=True)
    log.info(
        "detected language %s probability %s",
        getattr(info, "language", None),
        getattr(info, "language_probability", None),
    )
    parts: list[str] = []
    for segment in segments:
        text = segment.text.strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def telegram_url(cfg: Config, method: str) -> str:
    return f"https://api.telegram.org/bot{cfg.tg_bot_token}/{method}"


def ensure_telegram(response: httpx.Response) -> None:
    body = response.text[:500]
    if response.status_code != 200:
        raise RuntimeError(f"Telegram HTTP {response.status_code}: {body}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Telegram returned a non-JSON body") from exc
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError(f"Telegram rejected the request: {body}")


def send_message(client: httpx.Client, cfg: Config, text: str) -> None:
    response = client.post(
        telegram_url(cfg, "sendMessage"),
        json={"chat_id": cfg.tg_chat_id, "text": text},
    )
    ensure_telegram(response)


def send_document(client: httpx.Client, cfg: Config, file_id: str, text: str) -> None:
    require_id(file_id)
    response = client.post(
        telegram_url(cfg, "sendDocument"),
        data={"chat_id": cfg.tg_chat_id},
        files={
            "document": (
                f"{file_id}.txt",
                text.encode("utf-8"),
                "text/plain; charset=utf-8",
            )
        },
    )
    ensure_telegram(response)


def send_transcript(
    client: httpx.Client, cfg: Config, file_id: str, text: str
) -> None:
    body = text.strip()
    if not body:
        log.info("no speech in %s", file_id)
        send_message(client, cfg, NO_SPEECH)
        return
    if utf16_len(body) > TELEGRAM_TEXT_LIMIT:
        log.info("sending %s as a document (%s characters)", file_id, len(body))
        send_document(client, cfg, file_id, body)
        return
    log.info("sending %s as a message (%s characters)", file_id, len(body))
    send_message(client, cfg, body)


def delete_file(client: httpx.Client, cfg: Config, file_id: str) -> None:
    require_id(file_id)
    response = client.delete(
        f"{cfg.server_url}/files/{file_id}",
        headers=bearer(cfg.token),
    )
    if response.status_code == 404:
        log.info("id %s was already deleted", file_id)
        return
    if response.status_code != 204:
        raise RuntimeError(
            f"DELETE /files/{file_id} returned HTTP {response.status_code}"
        )


def remove_tree(path: Path) -> None:
    last_error: OSError | None = None
    for attempt in range(5):
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.2 * (attempt + 1))
    log.error("could not remove temp directory %s: %s", path, last_error)


def deliver(
    client: httpx.Client,
    cfg: Config,
    model,
    ffmpeg: str,
    file_id: str,
) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="transcribe-"))
    try:
        mp4 = tmp / f"{file_id}.mp4"
        wav = tmp / f"{file_id}.wav"
        download_file(client, cfg, file_id, mp4)
        extract_wav(ffmpeg, mp4, wav)
        text = transcribe_wav(model, wav)
        send_transcript(client, cfg, file_id, text)
    finally:
        remove_tree(tmp)


def list_files(client: httpx.Client, cfg: Config) -> list[dict]:
    response = client.get(f"{cfg.server_url}/files", headers=bearer(cfg.token))
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("GET /files did not return a list")
    return payload


def note_failure(state: State, cfg: Config, file_id: str, exc: BaseException) -> None:
    reason = redact(f"{type(exc).__name__}: {exc}", cfg)
    state.failures[file_id] = state.failures.get(file_id, 0) + 1
    state.reasons[file_id] = reason
    log.error(
        "id %s failed (%s/%s): %s\n%s",
        file_id,
        state.failures[file_id],
        MAX_FAILURES,
        reason,
        redact(traceback.format_exc().rstrip(), cfg),
    )


def give_up(
    client: httpx.Client, cfg: Config, state: State, file_id: str
) -> None:
    reason = state.reasons.get(file_id, "unknown error")
    text = clip_telegram(
        f"Failed 3 times, skipping until restart.\n{file_id}\n{reason}"
    )
    try:
        send_message(client, cfg, text)
    except Exception as exc:
        log.error(
            "could not send skip notice for %s: %s",
            file_id,
            redact(f"{type(exc).__name__}: {exc}", cfg),
        )
        return
    state.skipped.add(file_id)
    log.error("skipping %s until restart", file_id)


def handle_id(
    client: httpx.Client,
    cfg: Config,
    model,
    ffmpeg: str,
    state: State,
    file_id: str,
) -> None:
    if file_id in state.skipped:
        return
    if state.failures.get(file_id, 0) >= MAX_FAILURES:
        give_up(client, cfg, state, file_id)
        return
    try:
        if file_id in state.pending_delete:
            log.info("retrying delete for %s", file_id)
        else:
            log.info("processing %s", file_id)
            deliver(client, cfg, model, ffmpeg, file_id)
            state.pending_delete.add(file_id)
        delete_file(client, cfg, file_id)
    except FileGone:
        state.pending_delete.discard(file_id)
        log.info("id %s is no longer on the server", file_id)
        return
    except Exception as exc:
        note_failure(state, cfg, file_id, exc)
        if state.failures[file_id] >= MAX_FAILURES:
            give_up(client, cfg, state, file_id)
        return
    state.pending_delete.discard(file_id)
    state.failures.pop(file_id, None)
    state.reasons.pop(file_id, None)
    log.info("finished %s", file_id)


def poll_once(
    client: httpx.Client,
    cfg: Config,
    model,
    ffmpeg: str,
    state: State,
) -> None:
    for item in list_files(client, cfg):
        if not isinstance(item, dict):
            log.error("skipping a /files entry that is not an object")
            continue
        file_id = item.get("id")
        if not valid_id(file_id):
            key = repr(file_id)
            if key not in state.ignored:
                state.ignored.add(key)
                log.error("skipping invalid id %s", key)
            continue
        handle_id(client, cfg, model, ffmpeg, state, file_id)


def serve() -> None:
    log_path = Path(__file__).resolve().parent / "logs" / "worker.log"
    setup_logging(log_path)
    cfg = load_config()
    ffmpeg = ffmpeg_path()
    log.info("ffmpeg %s", ffmpeg)
    log.info(
        "polling %s every %s seconds with model %s",
        cfg.server_url,
        cfg.poll_seconds,
        cfg.model,
    )
    log.info("loading faster-whisper model %s on cuda float16", cfg.model)
    try:
        model = load_model(cfg.model)
    except Exception as exc:
        log.error(
            "model failed to load: %s",
            redact(f"{type(exc).__name__}: {exc}", cfg),
        )
        raise SystemExit(1) from None
    log.info("model loaded")
    state = State()
    timeout = httpx.Timeout(connect=30.0, read=600.0, write=120.0, pool=30.0)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        while True:
            try:
                poll_once(client, cfg, model, ffmpeg, state)
            except Exception as exc:
                log.error(
                    "poll failed: %s",
                    redact(f"{type(exc).__name__}: {exc}", cfg),
                )
            time.sleep(cfg.poll_seconds)


def main() -> None:
    try:
        serve()
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
