"""HTTP inbox for video uploads. The body is streamed to disk and never buffered whole."""

from __future__ import annotations

import logging
import os
import re
import secrets
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.requests import ClientDisconnect

log = logging.getLogger("uvicorn.error")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _die(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _load_token() -> str:
    token = os.environ.get("TOKEN", "")
    if len(token) < 32:
        _die(
            "Refusing to start: TOKEN must be at least 32 characters. "
            'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    return token


def _load_inbox() -> Path:
    raw = os.environ.get("INBOX_DIR", "inbox").strip()
    if not raw:
        _die("Refusing to start: INBOX_DIR is empty")
    return Path(raw).expanduser()


def _load_max_bytes() -> int:
    raw = os.environ.get("MAX_SIZE_MB", "500").strip()
    try:
        megabytes = int(raw)
    except ValueError:
        _die("Refusing to start: MAX_SIZE_MB must be an integer")
    if megabytes < 1:
        _die("Refusing to start: MAX_SIZE_MB must be >= 1")
    return megabytes * 1024 * 1024


def _load_port() -> int:
    raw = os.environ.get("PORT", "").strip()
    if not raw:
        _die("Refusing to start: PORT is not set")
    try:
        port = int(raw)
    except ValueError:
        _die("Refusing to start: PORT must be an integer")
    if not 1 <= port <= 65535:
        _die("Refusing to start: PORT must be between 1 and 65535")
    return port


TOKEN = _load_token()
INBOX = _load_inbox()
MAX_BYTES = _load_max_bytes()
PORT = _load_port()


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    INBOX.mkdir(parents=True, exist_ok=True)
    log.info(
        "transcribe inbox listening on 0.0.0.0:%s, dir %s, max %s bytes",
        PORT,
        INBOX,
        MAX_BYTES,
    )
    yield


app = FastAPI(
    lifespan=_lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


async def require_bearer(request: Request) -> None:
    header = request.headers.get("authorization", "")
    scheme, separator, provided = header.partition(" ")
    if not separator or scheme.lower() != "bearer":
        provided = ""
    try:
        matches = secrets.compare_digest(provided, TOKEN)
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise _unauthorized()


def _inbox_root() -> Path:
    return INBOX.resolve()


def _path_for(file_id: str) -> Path:
    if not _UUID_RE.fullmatch(file_id):
        raise HTTPException(status_code=400, detail="Invalid id")
    try:
        parsed = uuid.UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid id") from None
    if str(parsed) != file_id:
        raise HTTPException(status_code=400, detail="Invalid id")
    root = _inbox_root()
    raw = root / f"{file_id}.mp4"
    if raw.is_symlink():
        raise HTTPException(status_code=404, detail="Not found")
    resolved = raw.resolve()
    if resolved.parent != root:
        raise HTTPException(status_code=400, detail="Invalid id")
    return raw


def _declared_length(request: Request) -> int:
    raw = request.headers.get("content-length")
    if raw is None:
        raise HTTPException(status_code=411, detail="Content-Length required")
    if len(raw) > 20 or not raw.isdigit():
        raise HTTPException(status_code=400, detail="Invalid Content-Length")
    return int(raw)


def _discard_partial(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.exception("could not delete partial upload %s", path.name)


@app.put("/upload", status_code=201, dependencies=[Depends(require_bearer)])
async def upload(request: Request) -> JSONResponse:
    declared = _declared_length(request)
    if declared > MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large")

    file_id = str(uuid.uuid4())
    part_path = INBOX / f"{file_id}.mp4.part"
    final_path = INBOX / f"{file_id}.mp4"
    written = 0
    completed = False
    try:
        with part_path.open("wb", buffering=0) as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_BYTES:
                    raise HTTPException(status_code=413, detail="File too large")
                handle.write(chunk)
            os.fsync(handle.fileno())
        if written != declared:
            raise HTTPException(
                status_code=400,
                detail="Size does not match Content-Length",
            )
        os.rename(part_path, final_path)
        completed = True
    except ClientDisconnect:
        log.warning("dropped upload removed")
        raise HTTPException(status_code=400, detail="Upload interrupted") from None
    except HTTPException:
        raise
    except Exception:
        log.exception("upload failed")
        raise HTTPException(status_code=500, detail="Upload failed") from None
    finally:
        if not completed:
            _discard_partial(part_path)
    return JSONResponse(status_code=201, content={"id": file_id})


@app.get("/files", dependencies=[Depends(require_bearer)])
async def list_files() -> list[dict[str, str | int | float]]:
    root = _inbox_root()
    items: list[dict[str, str | int | float]] = []
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_file():
            continue
        if entry.suffix != ".mp4":
            continue
        file_id = entry.stem
        if not _UUID_RE.fullmatch(file_id):
            continue
        resolved = entry.resolve()
        if resolved.parent != root:
            continue
        stat = entry.stat()
        items.append({"id": file_id, "size": stat.st_size, "mtime": stat.st_mtime})
    items.sort(key=lambda item: (item["mtime"], item["id"]))
    return items


@app.get("/files/{file_id}", dependencies=[Depends(require_bearer)])
async def download(file_id: str) -> FileResponse:
    path = _path_for(file_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type="video/mp4")


@app.delete("/files/{file_id}", dependencies=[Depends(require_bearer)])
async def delete(file_id: str) -> Response:
    path = _path_for(file_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    path.unlink()
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
