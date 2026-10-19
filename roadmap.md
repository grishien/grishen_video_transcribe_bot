# roadmap.md — TikTok → VPS → PC (Whisper) → Telegram

## Agent rules (read first)
- Work only on the tasks (IDs like `1.2`) that I name in my message. Do not start others.
- When a task is done and verified, mark its item `[x]` in this file. If you could not finish it, leave `[ ]` and say what is blocking.
- Items labeled "Check" (`1.P`, `2.P`, ...) are ticked only by me, after my own verification.
- Do not invent APIs, versions or names. Use current stable versions of libraries and Gradle/AGP (as in the Android Studio template / official docs). If unsure, check the docs or ask me.
- Never commit secrets (token, server IP, bot token). Use `.env` / `local.properties` only, both in `.gitignore`. The repo contains `.env.example`.
- Code must be complete and working, no stubs and no "TODO: implement yourself".
- If something required is missing, ask me instead of inventing a value.

## Goal
On Android, in TikTok: "Save" → system "Send to" menu → my app. The mp4 is uploaded to a VPS. A Windows 11 PC picks the file up, transcribes it, sends the text to a Telegram bot and deletes the file from the VPS.

## Architecture
```
[Android app] --HTTP PUT--> [VPS: FastAPI/uvicorn on http://<VPS_IP>:<PORT>, inbox/ folder] <--HTTP poll/GET/DELETE-- [PC: Python worker] --> Telegram Bot API
```
No domain, no HTTPS, no reverse proxy (no Caddy/nginx). Plain HTTP directly to uvicorn, protected by a long random bearer token. Monorepo: `vps/`, `android/`, `pc/`.

## Environment (facts)
- PC: Windows 11, NVIDIA RTX 3090 Ti, Python. ffmpeg must be in PATH (if not, tell me how to install it).
- Videos: English and Russian, usually 20–120 seconds.
- Plain text only: no speaker diarization, no timestamps.
- VPS: Linux, SSH access, reachable by IP only (no domain). Other services already run on it (Amnezia, frp), so the receiver must use its own free port and must not touch existing services or their ports.
- Android Studio is installed. I will connect the phone via USB.

## Protocol (shared by all parts)
Every request carries `Authorization: Bearer <TOKEN>`. Compare with `secrets.compare_digest`. Missing or wrong token: 401.

| Method | Path | Behavior |
|---|---|---|
| PUT | `/upload` | Body = raw mp4 bytes (streamed). The server generates the id (uuid4), writes to `inbox/<id>.mp4.part`, after the full body is received verifies the size against `Content-Length`, then does an atomic `os.rename` to `inbox/<id>.mp4`. Response `201 {"id": "..."}`. On a broken upload the `.part` is deleted. Size limit is configurable (default 500 MB), 413 when exceeded. |
| GET | `/files` | Ready `*.mp4` files only (no `.part`): `[{"id","size","mtime"}]` |
| GET | `/files/{id}` | Download (streamed). `id` strictly validated as uuid, path traversal protection. |
| DELETE | `/files/{id}` | Delete. 204. |

The client-supplied file name is ignored.

---

## Phase 0 — Start
- [x] **0.1** `git init`, `.gitignore`, folder structure `vps/ android/ pc/`; ask me for the GitHub repository URL and connect it.

## Phase 1 — VPS receiver (`vps/`)
- [x] **1.1** FastAPI + uvicorn, `requirements.txt`, config from env: `TOKEN`, `INBOX_DIR`, `MAX_SIZE_MB`, `PORT`. Endpoints per the protocol table. Read the body only via `request.stream()`, never load it into memory. Provide a one-line command to generate the token (`python -c "import secrets; print(secrets.token_urlsafe(32))"`). Refuse to start if `TOKEN` is shorter than 32 characters.
- [x] **1.2** Deploy files: `deploy/transcribe-inbox.service` (systemd, unprivileged user, uvicorn listening on `0.0.0.0:$PORT`, env from a root-only env file). No Caddy, no nginx.
- [x] **1.3** `vps/README.md`: step-by-step deployment (user, venv, systemd unit); how to pick a free port (`sudo ss -tlnp` — the port must not be used by Amnezia, frp or anything else); how to open that port in the VPS firewall (and in the hosting provider's panel if it has one); a set of `curl` commands for verification. Explicitly state that existing Amnezia and frp setups must not be modified.
- [x] **1.P Check (me):** upload → `.mp4` appears in `inbox/`; visible in `/files`; downloadable; deletable; 401 without token; an interrupted upload leaves no `.mp4`; Amnezia and frp still work.

## Phase 2 — Android app (`android/`)
- [x] **2.1** Project: Kotlin, minSdk 26, targetSdk as in the current Android Studio template, single `app` module, no Compose. `SERVER_URL` (like `http://<VPS_IP>:<PORT>`) and `UPLOAD_TOKEN` via `local.properties` → `BuildConfig` (file in `.gitignore`). No settings screens.
- [x] **2.2** Cleartext HTTP is blocked by default on modern Android: add a `network_security_config` that permits cleartext traffic only for the VPS IP (take it from `local.properties` at build time or document where to put it; do not hardcode my IP into committed files).
- [x] **2.3** `MainActivity` with a no-UI theme handles `ACTION_SEND`, category `DEFAULT`, `mimeType="video/*"`. Gets the URI from `EXTRA_STREAM` (typed `getParcelableExtra` on API 33+), passes it to the service via `ClipData` + `FLAG_GRANT_READ_URI_PERMISSION` (otherwise access to the `content://` URI is lost), then calls `finish()` immediately.
- [x] **2.4** Foreground service (type `dataSync`; manifest permissions `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_DATA_SYNC`, `INTERNET`, `POST_NOTIFICATIONS`): reads the URI via `ContentResolver.openInputStream` and streams it to OkHttp `PUT /upload` with a custom `RequestBody`, without loading the file into memory. Timeouts sized for 50–200 MB over a mobile network.
- [x] **2.5** Notification with progress and the result ("Uploaded" / "Error: reason"). Up to 3 retries with increasing delay on network errors.
- [x] **2.6** Launcher screen: text "Share videos from TikTok here" and a notification permission request (Android 13+).
- [x] **2.7** A debug APK builds and installs on the phone.
- [ ] **2.P Check (me):** "Send to" from the gallery, then from TikTok → file on the VPS, notification shows the result.

## Phase 3 — PC worker (`pc/`)
- [ ] **3.1** Dependencies: `httpx`, `faster-whisper`, `python-dotenv`. `.env`: `SERVER_URL` (`http://<VPS_IP>:<PORT>`), `TOKEN`, `TG_BOT_TOKEN`, `TG_CHAT_ID`, `POLL_SECONDS=10`, `MODEL=large-v3`. Provide `.env.example`.
- [ ] **3.2** Loop: `GET /files` → download each new id to a temp folder → ffmpeg `-vn -ac 1 -ar 16000` to wav → faster-whisper (`device="cuda"`, `compute_type="float16"`, language auto-detect, `vad_filter=True`). The model is loaded once at startup.
- [ ] **3.3** Telegram delivery: `sendMessage`; text longer than 4096 characters is sent as a `.txt` via `sendDocument`; an empty result sends a "no speech found" message.
- [ ] **3.4** `DELETE` the file on the VPS only after a successful Telegram send. On any error the file stays and is retried on the next cycle. After 3 failures on one id: send me an error message in Telegram and skip that id until restart. Temp files on the PC are always removed (`finally`).
- [ ] **3.5** Logging to console and to a rotating file.
- [ ] **3.6** `pc/README.md`: installing dependencies; CUDA/cuDNN for faster-whisper (check the current faster-whisper/ctranslate2 README, do not guess versions); how to get `TG_CHAT_ID`; autostart via Windows Task Scheduler.
- [ ] **3.P Check (me):** drop an mp4 into `inbox/` manually → transcript in Telegram within ≤10 seconds, file gone from the VPS.

## Phase 4 — End-to-end test
- [ ] **4.1** Root `README.md`: diagram and a list of what must be configured by hand.
- [ ] **4.P Check (me):** TikTok → "Save" → "Send to" → Telegram with a Russian and an English clip; network drop during upload; PC switched off (the file waits on the VPS).