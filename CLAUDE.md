# Notes for AI agents working on this repo

Read this first, then [README.md](README.md) (setup, daily use) and [docs/architecture.md](docs/architecture.md)
(how every stage works, with measured numbers). This file is the *why* and the traps.

## What this is and why it exists
A one-person, local "clip factory": it watches YouTube streamers playing GTA 6, finds the moments chat exploded,
turns them into captioned 9:16 Shorts, reviews them with a local AI, and posts them to the owner's YouTube channel on
a schedule. It was built in November 2026 around the GTA 6 launch for four streamers (IShowSpeed, Kai Cenat Live, TGG,
Lacy). VOD mode (clipping past streams from chat replay) exists because the owner expects streamers to stop streaming
daily once launch hype fades: the factory must keep producing without live streams.

The owner runs it from the dashboard and PowerShell, reviews the output, and asks for changes in plain language.
Explain trade-offs plainly and verify behaviour on real data before claiming it works.

## Hard constraints (don't break these)
- **$0.** Free and open-source only. No paid APIs, no trials that turn paid, no cloud servers. The Google Cloud project
  has **no billing account** and the API key is restricted to YouTube Data API v3. Before proposing any new service,
  confirm it is free with no billing attached, and say so. Prefer local tools (Ollama, Whisper, FFmpeg).
- **Upload permission.** Only clips from channels with `permission_confirmed = 1` are ever uploaded. Never weaken this
  gate. Recording and clipping locally is allowed for any added channel.
- **Secrets.** Never print, log, commit or send: `.env` (API key, Telegram token), `data/client_secret.json`,
  `data/youtube_token.json`, `data/ntfy.json` (the topic is the only key to the alerts). `app/logsafe.py` redacts keys
  in logs; keep `httpx` at WARNING (it logs request URLs, which carry `?key=`). Write secret files only with
  `app/private_files.write_private` (owner-only ACL before any byte is written; existing ones are re-secured at
  startup by `secure_existing`). Treat the owner's personal details as private.
- **Public endpoints only.** Use public YouTube pages, the official Data API and yt-dlp/Streamlink. Reverse-engineering
  private YouTube endpoints (InnerTube) was declined by the owner.
- **Quota.** Stay well under 10,000 units/day (the app stops at 9,000). `search.list` is unused; `videos.list` is
  batched (1 unit per 50 IDs). The official live chat API costs ~1,780 units per stream-hour: fallback only.

## The owner's product decisions (keep unless they say otherwise)
- **Content:** "only GTA moments or extremely funny ones". Implemented as a chat filter for giveaways/polls
  (`spikes/detector.py: prompted_reason`). An AI rule "reject if no GTA on screen unless funny ≥ 8" was tried and
  **removed at the owner's request** because it rejected good clips (pause menu, post-credits scene). The AI still
  reports `gta_on_screen` and `funny` as information only. Don't reintroduce a hard rule like that.
- **Review before render:** the AI judges raw footage; only passed clips get the 9:16 render. Rejected clips' files are
  deleted 6 h later (time to overrule).
- **No nudity, ever:** a TGG clip with a nude cutscene character was approved and only caught by hand. Every clip's
  whole window is scanned with NudeNet (`qc/nudity.py`) before the AI review, and the upload queue refuses clips without
  `nudity_checked_at`. Never weaken this gate. **License:** NudeNet's files are AGPL-3.0 (its metadata says MIT); the
  owner chose it knowing a license on this repo would then have to be AGPL-compatible.
- **Operator note:** per-clip "tell the AI what's on screen", trusted over the model's own reading (small model, 6 frames).
- **Posting:** 6 slots on the US evening (US Eastern), 6/day, a breaking lane for live clips ≥ 8 within 2 h, freshness
  and variety in the pick, expiry after 96 h. The owner chose these; see `PostingStrategy`.
- **Alerts:** ntfy, because the owner's ISP blocks Telegram's bot API (Telegram code is kept as an alternative).
- **Past streams without chat replay** keep being retried (hourly ×6) rather than given up early: the owner asked to
  undo a "give up after 12 h" change because replay can appear after processing.
- **Open requests:** smart trimming (clips are a fixed 40 s; the owner wants the main part), longer chat-replay retry,
  audio-loudness fallback for streams without chat replay. More in [docs/future-ideas.md](docs/future-ideas.md).

## How the code is organised
`app/main.py` wires everything (`build_services`) and registers the scheduler jobs; each stage is a worker class with
a `run_once`/`run_all` that picks rows from SQLite by status. Read [docs/architecture.md](docs/architecture.md) for the
job table, clip status machine and data model. Key ideas an edit must respect:
- **One time base per clip:** `window_*_pts` and `source_start_pts` are stream PTS for live clips and VOD seconds for
  past-stream clips; every stage uses `window_start_pts − source_start_pts`. Chat and spikes use YouTube-clock epoch seconds.
- **Live first:** workers order by `source_kind = 'vod'` and handle at most one VOD clip per run; VOD work pauses while
  recording. Keep that when adding stages.
- **Resumable:** every status that means "busy" must be reset in `Database.mark_interrupted()`.
- **Outages don't burn attempts:** wrap network failures with `app/net.py: is_network_error` and retry without
  counting an attempt (downloads, VOD analysis, uploads already do).
- **Migrations are append-only** (`app/db.py: MIGRATIONS`). Never edit an applied migration; add a new entry. The
  owner's database is at version 16.
- **Spikes have a `source`** (live/vod): `plan_clips` takes live ones, `VodWorker.plan` takes VOD ones.

## Running and testing
```powershell
.\.venv\Scripts\python -m app                 # start (NOT `uvicorn app.main:app`: see traps)
.\.venv\Scripts\python -m pytest -q           # ~215 tests, ~2 min; ffmpeg-dependent ones need ffmpeg on PATH
```
Tests use fakes for YouTube, yt-dlp, Ollama and Whisper (`tests/conftest.py`, `tests/test_vod.py: FakeTools`) and
synthetic video from ffmpeg `lavfi`. `tests/test_e2e.py` runs one synthetic stream through every stage.

**Verify on real data, safely.** Unit tests are not enough for YouTube-facing changes; run the real thing on a
**copy** of the database: `sqlite3.connect(src).backup(dst)`, then `Settings(data_dir=<scratch>)` or start the app with
`DATA_DIR=<scratch> PORT=8011 PUBLISH_ENABLED=false`. Never write to `data/clipper.db` while the owner's app is running
with different code (e.g. old code would grab VOD spikes as live ones). Check `Get-NetTCPConnection -LocalPort 8000`.

## Traps on this machine (each cost real time)
- **Shell backslashes:** heredocs in the Bash tool collapsed `\\1` / `\\n` into control characters or real newlines
  inside Python source (twice in regexes, once in a string). Write scripts with the file-writing tool, or build the
  backslash with `chr(92)`, and re-read the written line. Heredocs containing backticks also failed to parse.
- **Console encoding is cp1252:** printing stream titles with emoji crashes. Use `sys.stdout.reconfigure(encoding="utf-8")`;
  `python -I` ignores `PYTHONIOENCODING`.
- **FFmpeg from WinGet is a symlink** on PATH; yt-dlp given that folder fails with exit code 3436169992. Use the resolved
  path (`vod.ffmpeg_location`).
- **yt-dlp needs Deno + `yt-dlp-ejs`** for YouTube's JS challenges. "Sign in to confirm you're not a bot" / 403 →
  update yt-dlp and streamlink first.
- **DASH section downloads stall** (minutes for seconds of video); HLS (`protocol^=m3u8`) sections take ~1 min.
- **Ctrl+C:** with `uvicorn app.main:app`, Python waited for running jobs (minutes) and a second Ctrl+C skipped cleanup.
  `app/__main__.py` exits as soon as the server stops. On Windows `.venv\Scripts\python.exe` is a launcher that spawns
  the real interpreter: when checking whether the app is gone, match processes by command line, not the launcher PID.
- **Python 3.12 lives in a uv-managed install** on the owner's machine; always use `.venv\Scripts\python`.
- **Hugging Face is blocked/rate-limited** on this network: `WHISPER_BACKEND=openai` (weights from OpenAI's CDN).
- **Telegram's bot API is blocked by the ISP** (some IPs time out); ntfy works.
- **YouTube RSS lags** behind going live; the `/live` page probe catches it. A session marked ended while still live is
  reopened by the monitor's UPSERT.
- **Chat replay JSON:** `videoOffsetTimeMsec` sits inside `replayChatItemAction` in replay files but at the top level in
  live files (`LineParser.parse_obj` handles both).
- **Quota table changed in 2026:** `videos.insert` and `search.list` cost 1 unit each from their own 100/day
  allowances (checked 2026-11-23). Re-check the official quota page rather than trusting memory.

## Working style the owner responded well to
- Measure before answering ("how long does X take" → run it on a real stream), and show the numbers.
- Ask before decisions that are theirs (posting times, how aggressive filters are); otherwise pick a sensible default
  and say so.
- Keep live recording safe: anything heavy or network-hungry waits while a stream is being recorded.
- Commit only when asked; never commit `.env`, `data/` or `.claude/settings.local.json`.
