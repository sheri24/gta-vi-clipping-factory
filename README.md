# GTA VI Clip Factory

A local, free pipeline that turns GTA 6 livestreams into YouTube Shorts on its own:

```
watch YouTube streamers → record live → read chat → find the moments chat exploded
→ cut the moment → transcript → local AI review → 9:16 edit with facecam + captions
→ title & hashtags → upload on a posting schedule → phone alert
```

While nobody is live it does the same with the streamers' **past streams**, using the chat replay YouTube keeps
with every stream. Everything runs on one Windows laptop: no cloud servers, no paid APIs, no AI subscriptions.

> **Permission first.** Re-uploading someone else's stream is copyright infringement unless they agreed. The app
> records and clips any channel you add, but only **uploads** clips from channels you've marked **Upload allowed**,
> which you should only do with the creator's permission. Every Short credits the streamer and links to the moment.

## Contents
- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Setup from scratch](#setup-from-scratch)
- [Daily use](#daily-use)
- [Costs ($0) and safety](#costs-0-and-safety)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- Deeper docs: [architecture](docs/architecture.md) · [Google Cloud setup](docs/setup-google-cloud.md) ·
  [YouTube upload setup](docs/setup-youtube-upload.md) · [future ideas](docs/future-ideas.md) ·
  [original research](docs/phase-1-research.md) · [notes for AI agents](CLAUDE.md)

## What it does
| Stage | How |
|---|---|
| **Find live streams** | Each channel's RSS feed and `/live` page (free), confirmed with one batched YouTube API call per minute. |
| **Record** | Streamlink (yt-dlp as fallback) into 60 s pieces; only the last 30 min are kept while live. |
| **Read chat** | yt-dlp follows live chat for free (the official API is the fallback). |
| **Find moments** | A spike in *distinct chatters* per 10 s, judged against that stream's own normal. Giveaways and polls (everyone typing the same word) are ignored. |
| **Past streams** | While nobody is live: the chat replay of each finished GTA 6 stream goes through the same spike detector, and only ~45 s around each moment is downloaded, never the whole stream. |
| **Cut + transcript** | The moment's footage, then Whisper speech-to-text on the laptop. |
| **AI review** | A local vision model (Ollama) scores frames + transcript + chat. Only clips that pass get edited, so rejected clips cost no render. |
| **Edit** | 1080×1920: facecam on top and gameplay below when a webcam is found (blurred full frame otherwise), word-by-word captions, `@channel` credit, −14 LUFS. |
| **Publish** | Title/description/hashtags from a local LLM, uploaded to YouTube in 6 daily slots on the audience's evening; a very strong live moment posts at once. |
| **Dashboard** | http://127.0.0.1:8000/: live chat traces, clips with player and AI reasoning, past streams, upload queue, channel switches. |
| **Alerts** | ntfy push notifications: streamer live, clip approved, Short uploaded, problems. |

It was built for four streamers during the GTA 6 launch (IShowSpeed, Kai Cenat, TGG, Lacy), but any YouTube
channel works. How each stage works, with the numbers measured while building it: [docs/architecture.md](docs/architecture.md).

## Requirements
- **Windows 10/11** (it uses Windows job objects and was only run there). 16 GB RAM minimum, 32 GB comfortable.
  No GPU needed: everything runs on the CPU (built on an Intel i5-1245U laptop).
- **~25 GB free disk**: ~6 GB for tools and models, ~1–2 GB per stream being recorded, a few MB per clip.
- **Python 3.12**, **FFmpeg** (with libass), **Deno** (yt-dlp needs it to solve YouTube's download challenges),
  **Ollama**, **Git**. Installed below.
- A **Google account**, for a free YouTube API key, and the YouTube channel the Shorts go to.

## Setup from scratch
About 30–45 minutes, mostly downloads. Commands are for PowerShell.

### 1. Install the tools
```powershell
winget install --id Git.Git -e
winget install --id Gyan.FFmpeg -e            # full build: includes libass for captions
winget install --id DenoLand.Deno -e            # JavaScript runtime yt-dlp uses for YouTube
winget install --id Ollama.Ollama -e
winget install --id astral-sh.uv -e           # easiest way to get Python 3.12
```
Close and reopen PowerShell so the new commands are on PATH, then check:
```powershell
ffmpeg -version          # "ffmpeg version 7" or newer
deno --version
ollama --version
```

### 2. Get the code and a Python environment
```powershell
cd C:\                                      # or any drive with space, e.g. D:\
git clone https://github.com/<you>/<repo>.git gta-vi-clipping
cd gta-vi-clipping
uv venv --python 3.12 --seed .venv              # downloads Python 3.12 if needed; --seed adds pip
.\.venv\Scripts\python -m pip install -r requirements.txt     # ~2 GB: PyTorch CPU for Whisper
```
(Without uv: install Python 3.12 from python.org and use `py -3.12 -m venv .venv` for the second line.)

### 3. The local AI model
```powershell
ollama pull gemma4:e2b        # ~4.6 GB; reviews clips and writes titles. Must accept images.
```
The Ollama app keeps running in the background after install. The Whisper speech model (~460 MB) and the face
detector (~230 KB) download by themselves on first use into `data/models/`.

### 4. A YouTube API key (free, ~5 min)
Follow [docs/setup-google-cloud.md](docs/setup-google-cloud.md): create a Google Cloud project, enable YouTube Data
API v3, create an API key restricted to it. **Don't add billing.** Then:
```powershell
copy .env.example .env
notepad .env                  # paste the key into YOUTUBE_API_KEY=
```
Every other setting in `.env` has a working default and is explained in the file.

### 5. First start
```powershell
.\.venv\Scripts\python -m app
```
Open **http://127.0.0.1:8000/**. In **Channels**, paste a streamer's channel link (e.g.
`https://www.youtube.com/@LacyHimself`) and click **Add streamer**. From now on their live streams are recorded and
clipped, and their GTA 6 streams from the last 3 days are clipped from the chat replay. Nothing is uploaded yet.

Run the tests once to check the install: `.\.venv\Scripts\python -m pytest -q` (all should pass, ~2 min).

### 6. Uploading to YouTube (optional, ~10 min)
Follow [docs/setup-youtube-upload.md](docs/setup-youtube-upload.md): an OAuth desktop client in the same Google
project, then a one-time sign-in:
```powershell
.\.venv\Scripts\python -m app.publish.auth
```
Then set `PUBLISH_ENABLED=true` in `.env`, restart, and switch on **Upload allowed** for each channel whose creator
agreed. While the Google app is in *Testing* mode the sign-in lasts 7 days; the dashboard shows when to renew it.

### 7. Phone alerts (optional, ~2 min, free)
Install the **ntfy** app on your phone, then:
```powershell
.\.venv\Scripts\python -m app.notify.ntfy
```
It creates a private topic, saves it to `data/ntfy.json`, and sends a test alert. In the ntfy app tap **+**, enter
that topic, **Subscribe**. Run this once only: running it again makes a new topic. Telegram is supported too
(`TELEGRAM_BOT_TOKEN` in `.env`, then `python -m app.notify.telegram`), but some ISPs block Telegram's bot servers.

### 8. Leaving it running unattended (optional)
Posting slots fall at night in some time zones and streams start whenever they start, so the laptop must stay awake.
Plug it in, then stop it sleeping on power, including with the lid closed:
```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
```

## Daily use
**Start:** `.\.venv\Scripts\python -m app` in the project folder. Leave the window open (minimised is fine).

**Stop:** **Ctrl+C**. It exits within a second or two and prints "Clip factory stopped". Work in progress resumes
from the same step next time, so stopping midway is safe.

**Dashboard** (http://127.0.0.1:8000/, refreshes itself):
- **Top bar**: streams live, API quota used today, free disk, YouTube connection and sign-in expiry, uploads on/off.
- **On air**: one strip per channel; live streams show 30 min of chat activity with spikes and the clips they made.
- **Past streams**: finished streams being clipped, in order, with what happened to each. Paste a link to clip a
  specific stream; **Try again** / **Skip** per stream.
- **Clips**: every clip with its stage and score. Click one for the player, the AI's scores and reasoning, transcript
  and chat. **Approve** / **Reject** overrule the AI. **Tell the AI what's on screen** adds a note (e.g. "GTA 6
  pause menu, then the post-credits scene") that the review and the title writer trust, then reviews again.
- **Publishing**: the queue with the next posting slot, and the upload history.
- **Channels**: switches per channel: *Watch*, *Record & clip*, *Past streams*, *Upload allowed*, layout, chat delay.
- **System**: storage, recent problems, the log.

**Posting schedule** (defaults; `PUBLISH_SLOTS` in `.env`): 12:00, 14:30, 17:00, 19:00, 21:00, 23:00 US Eastern,
one Short per slot, max 6 a day. Fresh moments beat slightly better old ones, the same streamer isn't posted twice in
a row, and a live clip scoring 8+ within 2 hours of the moment posts immediately. Clips over 4 days old are dropped.

**Logs**: `data/logs/app.log` (keys and tokens are redacted). **Database**: `data/clipper.db` (SQLite).

## Costs ($0) and safety
| Service | Used for | Can it cost money? |
|---|---|---|
| YouTube Data API v3 (API key) | live detection, past-stream lookup, chat fallback | No. There is no paid tier: past the daily quota (10,000 units) Google refuses requests until midnight Pacific. The app stops itself at 9,000 (`QUOTA_DAILY_BUDGET`); normal use is ~1,500/day. |
| YouTube Data API v3 (OAuth) | uploads | No. 1 unit per upload from a separate allowance of 100 uploads/day. |
| Ollama, Whisper, FFmpeg, Streamlink, yt-dlp, OpenCV | AI review, captions, video, recording | No. Local, open-source. |
| ntfy.sh | phone alerts | No. Free public service, no account. |

The only way to ever be charged would be a billing account linked to the Google project **and** the API key leaking
and being used on a paid Google API. Close both doors (replace `<project-id>` with yours):
1. https://console.cloud.google.com/billing/linkedaccount?project=<project-id> should say *"This project has no
   billing account"*.
2. https://console.cloud.google.com/apis/credentials?project=<project-id> → your key → *API restrictions* → only
   **YouTube Data API v3**.

**Secrets stay local and out of git** (`.gitignore` covers them): `.env` (API key, tokens), `data/client_secret.json`
and `data/youtube_token.json` (YouTube sign-in), `data/ntfy.json` (your alert topic is the only key to it).
The app also restricts these files to your Windows account (on a secondary drive, files are otherwise readable by
every account on the PC), when it writes them and again at every start.

## Troubleshooting
| Symptom | Fix |
|---|---|
| Clips stay at "Waiting for AI review" | Ollama isn't running or the model is missing: open the Ollama app; `ollama list` should show `gemma4:e2b`. |
| Clips stay at "Waiting for transcript" | First run: Whisper is downloading its model (~460 MB). If Hugging Face is blocked, keep `WHISPER_BACKEND=openai` (downloads from OpenAI's CDN). |
| "YouTube not connected" / sign-in expired | `.\.venv\Scripts\python -m app.publish.auth` (every 7 days while the Google app is in Testing mode). |
| A channel shows offline while it's live | Click **Check now**. Detection runs every minute; YouTube's RSS can lag, the `/live` page probe catches most streams. |
| A past stream says "No chat replay" | The streamer turned chat replay off (or YouTube hasn't processed it yet: it's retried hourly for 6 h). Those streams can't be clipped yet. |
| Recording or downloads fail with HTTP 403 / "Sign in to confirm" | Update the downloaders: `.\.venv\Scripts\python -m pip install -U yt-dlp streamlink`. YouTube changes often. |
| Internet dropped | Nothing to do: monitoring, recording, downloads and uploads wait and continue on their own; an outage never uses up retry attempts. |
| Uploads arrive private | Unaudited Google projects can be locked to private uploads. The app checks 10 min after each upload and alerts you with an *Open in Studio* button; see [the upload guide](docs/setup-youtube-upload.md). |

## Project layout
```
app/
  __main__.py        start command (python -m app); exits cleanly on Ctrl+C
  main.py            FastAPI app, service wiring, scheduler jobs, HTTP API
  config.py          every setting (read from .env)
  db.py              SQLite + append-only migrations
  monitor.py         live detection          channels.py   add/remove channels
  recording/         recorder, retention, PTS↔UTC timeline
  chat/              live chat sources, storage, 10 s buckets
  spikes/            spike detector + giveaway/poll filter
  vod.py             past-stream mode
  clips/             cut, facecam detection, 9:16 render
  captions/          Whisper transcript, captions, final render
  qc/                objective checks + Ollama review
  publish/           titles, YouTube upload, posting strategy
  notify/            ntfy / Telegram alerts
  static/            the dashboard (plain HTML/CSS/JS, no build step)
tests/               pytest suite (synthetic streams, fakes for YouTube/Ollama/yt-dlp)
docs/                setup guides, architecture, research, ideas
```
