# PC worker

Polls the VPS inbox, turns each mp4 into 16 kHz mono audio with ffmpeg, transcribes it with faster-whisper on the GPU, sends the text to Telegram, then deletes the file.

Python 3.10 or newer. `python-dotenv` 1.2.4 requires 3.10+, and `faster-whisper` 1.2.1 requires 3.9+.

```powershell
cd pc
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

That installs `httpx` 0.28.1, `faster-whisper` 1.2.1, and `python-dotenv` 1.2.4. faster-whisper pulls `ctranslate2` 4.8.3.

## ffmpeg

The worker shells out to `ffmpeg -vn -ac 1 -ar 16000`. Check:

```powershell
ffmpeg -version
```

If PowerShell reports that the command is not recognized:

```powershell
winget install Gyan.FFmpeg
```

Open a new terminal and run `ffmpeg -version` again. The worker exits at startup when `ffmpeg` is missing or `ffmpeg -version` fails.

## NVIDIA GPU

Run the worker on the PC with the RTX 3090 Ti. The model is loaded once, with `device="cuda"` and `compute_type="float16"`.

Versions below are from the faster-whisper 1.2.1 README, the CTranslate2 4.8.3 installation page and changelog, and the Windows wheel installed by the requirements file.

faster-whisper requires:

- [cuBLAS for CUDA 12](https://developer.nvidia.com/cublas)
- [cuDNN 9 for CUDA 12](https://developer.nvidia.com/cudnn)

Current `ctranslate2` (4.5.0 and later, including 4.8.3) is built for CUDA 12 and cuDNN 9. The CTranslate2 installation page still says cuDNN 8 for speech models. That sentence is older than the [v4.5.0 changelog](https://github.com/OpenNMT/CTranslate2/blob/master/CHANGELOG.md) (2024-10-22), which says the wheels moved to cuDNN 9. Install cuDNN 9.

The `ctranslate2` 4.8.3 Windows wheel ships `cudnn64_9.dll` next to the package and imports `cublas64_12.dll`. cuBLAS is not in the wheel. `cublas64_12.dll` has to be on `PATH`.

The Linux commands `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` in the faster-whisper README are for Linux. On Windows use one of the two installs below.

### Driver

Install the current driver for the RTX 3090 Ti from [NVIDIA's driver download](https://www.nvidia.com/Download/index.aspx). CUDA 12 needs that current driver. `nvidia-smi` prints the driver version. A driver that is too old fails later with `CUDA driver version is insufficient for CUDA runtime version`.

### NVIDIA installers

1. [CUDA Toolkit 12.x](https://developer.nvidia.com/cuda-downloads) for Windows x86_64. The installer adds `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.x\bin` to `PATH`. That directory contains `cublas64_12.dll`.
2. [cuDNN 9 for CUDA 12](https://developer.nvidia.com/cudnn). NVIDIA's Windows instructions copy the libraries to `C:\Program Files\NVIDIA\CUDNN\v9.x\` and add `C:\Program Files\NVIDIA\CUDNN\v9.x\bin` to `PATH`.

### Purfview archive

faster-whisper also documents the libraries from [Purfview's `libs` release](https://github.com/Purfview/whisper-standalone-win/releases/tag/libs). Download `cuBLAS.and.cuDNN_CUDA12_win_v3.7z`. The release lists it as cuBLAS 12.8.4.1 and cuDNN 9.8.0.87. Extract it (7-Zip from [7-zip.org](https://www.7-zip.org/) when Explorer cannot open `.7z`) and add the directory that contains `cublas64_12.dll` to your user `PATH`.

Use that v3 archive. `cuBLAS.and.cuDNN_CUDA12_win_v1.7z` on the same page is cuDNN 8.9.7.29, and ctranslate2 4.5.0 and later does not load cuDNN 8.

### Visual C++

The CTranslate2 installation doc says the Windows wheel needs the Visual C++ runtime, which is already installed on most PCs. If import fails with a missing `VCRUNTIME140.dll` or `MSVCP140.dll`, install the x64 build from [the latest supported Visual C++ redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist).

### Check

Close the terminal and open a new one after any `PATH` change, then:

```powershell
.\.venv\Scripts\python.exe -c "import ctranslate2; print(ctranslate2.get_cuda_device_count()); print(ctranslate2.get_supported_compute_types('cuda'))"
```

A usable GPU prints a count of at least 1, and the set of types includes `float16`.

## Configure

```powershell
copy .env.example .env
```

Edit `pc\.env`. One `KEY=VALUE` per line, no quotes, no spaces around `=`. The file is gitignored.

| Key | Value |
|---|---|
| `SERVER_URL` | Inbox base address, such as `http://203.0.113.10:8080`. No path, no username. |
| `TOKEN` | The same bearer token as `/etc/transcribe-inbox.env` on the VPS. At least 32 characters. |
| `TG_BOT_TOKEN` | Token from [@BotFather](https://t.me/BotFather). |
| `TG_CHAT_ID` | Numeric chat id from the steps below. |
| `POLL_SECONDS` | `10` |
| `MODEL` | `large-v3` |

The process refuses to start when `.env` is missing, `TOKEN` is shorter than 32 characters, or `SERVER_URL` is empty.

### TG_CHAT_ID

1. In Telegram, open @BotFather, create a bot, and put its token in `TG_BOT_TOKEN`.
2. Open that bot and send it any message. A bot cannot write to you first.
3. Open this URL in a browser, with your token in place of `<TG_BOT_TOKEN>`:

```text
https://api.telegram.org/bot<TG_BOT_TOKEN>/getUpdates
```

4. `chat.id` in that JSON is `TG_CHAT_ID`. A private chat id is a positive number. A group id is negative.

If `result` is empty, send the bot another message and reload the URL. A new bot has no webhook, so `getUpdates` returns the message you sent.

## Run

```powershell
.\.venv\Scripts\python.exe worker.py
```

The first start downloads [Systran/faster-whisper-large-v3](https://huggingface.co/Systran/faster-whisper-large-v3) into `%USERPROFILE%\.cache\huggingface\hub`. Wait until the log says `model loaded`. The model stays loaded until the process exits. Ctrl+C stops it.

The same lines are printed to the console and appended to `pc\logs\worker.log` (2 MB, then rotated, 5 old files kept). The inbox token and the bot token are stripped out of those lines.

`GET /files` lists only ready `*.mp4` names that are a lowercase UUID. To drop a file in by hand:

```powershell
py -3 -c "import uuid; print(uuid.uuid4())"
```

Copy the video to `/var/lib/transcribe-inbox/<that-uuid>.mp4` on the VPS. On the next poll the worker downloads it, transcribes, sends the text, and deletes it. Have the worker already sitting on `model loaded` before you copy the file; the download of the weights takes longer than one poll.

Telegram receives:

- the transcript as a plain message, without timestamps
- a `<id>.txt` document when the text is longer than 4096 characters
- `no speech found` when the transcript is empty

The VPS file is deleted only after Telegram accepts that message. A failed step leaves the file in the inbox and the next poll tries it again. After 3 failures of the same id, the worker sends `Failed 3 times, skipping until restart.` and ignores that id until the process is started again. If Telegram already accepted the transcript and only the delete failed, the next poll deletes the file and does not send the text a second time. Temp files on this PC are removed at the end of every attempt.

## Start at logon

The worker is one long-running process. CUDA is available in the logged-on desktop session, so the task has to run as you, while you are logged on.

1. Open Task Scheduler (`taskschd.msc`).
2. Create Task.
3. General: name it `Transcribe worker`. Select **Run only when user is logged on**.
4. Triggers, New: begin **At log on**, for your user. Delay the task for 1 minute so the network is up.
5. Actions, New, Start a program:
   - Program/script: the full path to `pc\.venv\Scripts\python.exe`
   - Add arguments: the full path to `pc\worker.py`
   - Start in: the full path to `pc`
6. Settings: if the task is already running, **Do not start a new instance**. If the task fails, restart it every 1 minute.
7. OK.

A second copy would send the same transcript twice. After you change `PATH` for CUDA, sign out and back in so the scheduled task sees the new path. End that `python.exe` process, or disable the task, to stop the worker.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest test_worker -v
```

These cover polling, the Telegram size rules, delete-after-send, and the ffmpeg wav. They do not load the GPU model.
