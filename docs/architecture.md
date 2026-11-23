# Architecture

How each stage of the clip factory works, why it works that way, and the numbers measured while building it
(November 2026, on a 15 W Intel i5-1245U laptop with no GPU). Settings in `CAPS` live in `.env` / `app/config.py`.

## The big picture
```
                ┌──────────── live (while a stream is on) ───────────┐
RSS + /live ──► monitor ──► recorder (60 s .ts pieces, 30 min buffer)  │
page + API       │          chat collector (yt-dlp) ──► 10 s buckets ──┤
                 │                                                     ▼
                 │                                   spike detector (+ giveaway/poll filter)
                 │                                                     │
Live tab ──► VOD scanner ──► chat replay ──► same buckets & detector ──┤   (past streams, while nobody is live)
                                                                       ▼
                                   clip: get footage (local pieces, or a downloaded section)
                                     → Whisper transcript → AI review (Ollama) ─► rejected (files deleted after 6 h)
                                     → passed → 9:16 render with facecam + captions → approved
                                     → posting strategy (slots / breaking lane) → YouTube upload → ntfy alert
```
One process (`python -m app`) runs a FastAPI server (dashboard + HTTP API) and an APScheduler with these jobs:

| Job | Every | Does |
|---|---|---|
| `monitor` | 60 s | live detection; starts/stops recorders and chat collectors |
| `anchors` | 10 min | re-syncs recording time to YouTube's clock |
| `chat_analysis` | 10 s | recomputes chat buckets, runs spike detection on live streams |
| `clips` | 15 s | facecam detection, plans clips from spikes, gets their footage |
| `post` | 20 s | transcript → AI review → render what passed |
| `publish` | 60 s | posting strategy + upload |
| `notify` | 30 s | phone alerts |
| `retention` | 60 s | deletes old footage and finished/rejected clips |
| `vod_scan` | 30 min | finds finished streams |
| `vod` | 60 s | analyses one past stream at a time |

Every job has `max_instances=1`, so a slow run never overlaps the next. Heavy work (renders, Whisper, Ollama) runs
one clip at a time: the CPU is a 15 W part.

**Live comes first.** Past-stream work pauses while anything is recording; the cutter, transcriber, AI review and
renderer take live clips first and at most one past-stream clip per round; in the upload queue a past-stream clip
ranks 1 point below a live clip with the same score. A backlog of past-stream clips never delays a fresh live moment by
more than one clip.

**Everything resumes.** All state is in SQLite (`data/clipper.db`). On startup `Database.mark_interrupted()` moves
anything that was mid-step back one step (e.g. `rendering → passed`), so stopping the app at any moment is safe.

**Internet outages never use up retries.** `app/net.py` tells "the internet is down" apart from real failures;
outages make downloads, analysis and uploads wait and retry without counting an attempt.

## 1. Live detection (`app/monitor.py`)
Every `POLL_INTERVAL_SECONDS` (60):
1. **Free signals:** each enabled channel's RSS feed (latest 15 videos) plus its `/channel/<id>/live` page, whose
   canonical link points at the live video while the channel is live. RSS alone misses some streams.
2. **Confirmation:** every new, live or upcoming candidate goes into one batched `videos.list`: **1 quota unit per 50
   videos**. Finished videos are remembered in `videos_seen` and never asked about again.
3. **Sessions:** `live_sessions.video_id` is UNIQUE, so a stream opens one session. When the API fails or the quota
   is spent, sessions are left alone: missing data is not proof a stream ended. A session marked ended while YouTube
   still says live (a long outage, a channel removed and re-added) is reopened.

Spend: ~1,440 units/day at a 60 s poll, for up to 50 candidates. The app never uses `search.list`.

## 2. Recording (`app/recording/`)
- **Gate:** recorded only if the channel has *Record & clip* on and the disk has `MIN_FREE_GB` (10) free.
- **Pipeline:** `streamlink --stdout` → `ffmpeg -copyts -c copy -f segment` → `data/recordings/<video_id>/seg_000001.ts`
  (60 s each). Stream copy, no re-encoding: negligible CPU.
- **Recovery:** on a drop the recorder restarts with backoff (2 s after a healthy run, 5→60 s after short ones); after
  2 failed Streamlink runs it switches to yt-dlp and back. Numbering continues; nothing is overwritten.
- **Time sync (`timeline.py`):** every 10 min one ~2 s HLS segment is fetched; its `EXT-X-PROGRAM-DATE-TIME` (YouTube's
  UTC) is paired with its PTS, giving `utc = anchor.utc + (pts − anchor.pts)`. Measured drift: 5 ms over 60 s. The
  laptop clock is never used (it was 4–5 s off).
- **Chat delay:** the same sample measures how far the live edge trails real time. Chat reacts at
  `edge lag + one segment + ~3 s typing`: predicted 39.8 s vs ~39 s observed on Lacy's GTA VI finale.
- **Storage:** while live only the last `BUFFER_MINUTES` (30) are kept; `END_CLEANUP_GRACE_MINUTES` (10) after a stream
  ends all its footage is deleted. Segments a pending clip still needs are never deleted. 720p60 ≈ 1–2.8 GB/hour,
  so ~0.5–1.4 GB on disk per live stream at any moment.
- **Child processes:** killed as a tree (`taskkill /T`), and the app sits in a kill-on-close Windows job object, so no
  orphaned ffmpeg/streamlink/yt-dlp survives a crash.

## 3. Live chat (`app/chat/`)
- A collector runs for every enabled live channel (reading chat doesn't re-publish anything).
- **Sources** (`CHAT_SOURCES=ytdlp,official`), tried in order; after 3 quick failures the collector moves to the next,
  and returns to the free one after 5 min:
  - **ytdlp**: free, unofficial; follows the chat and writes JSON lines, which are tailed. It sees YouTube's default
    **Top chat** view, about **71%** of messages (short/repeated ones are hidden). Enough for spikes.
  - **official**: gRPC `liveChatMessages.streamList` with the API key. YouTube closes each stream after ~10.5 s, so it
    costs ~**1,780 units per stream-hour**: a fallback only.
- **Buckets:** 10 s windows on YouTube's publish time: messages, **distinct authors**, paid, memberships, gifts.
  Recomputed every 10 s for the last 2 min so late messages land in the right bucket.
- Raw messages are deleted with the stream's footage; buckets are kept.
- Small YouTube chats make no spikes: xQc's YouTube chat averaged 1.3 chatters per 10 s (he streams mainly on Twitch).

## 4. Spike detection (`app/spikes/detector.py`)
- **Signal:** distinct chatters per 10 s bucket, so one spammer can't fake a spike.
- **Baseline:** the median of the surrounding chat, **5 min before and 3 min after**, skipping ±60 s around the
  bucket itself. Looking both ways matters: when we join mid-hype there is no "before" (a past-only baseline missed the
  shooting in Lacy's finale; that data is a regression test).
- A bucket is **hot** at ≥ 3.5 robust z (MAD scale, floored at √median) **and** ≥ 1.6× the median **and** ≥ 5 chatters.
  Hot buckets less than 20 s apart are one moment.
- Spikes stay `open` until their 3-minute "after" window has arrived, then `closed` (clippable): ~3–4 min latency.
- **Clip window:** `moment = spike start − chat delay`, `clip = [moment − 15 s, moment + 25 s]` (`CLIP_PRE_SECONDS`,
  `CLIP_POST_SECONDS`). Every clip is 40 s; smarter trimming is an open idea.
- **Giveaways and polls:** on Lacy's finale VOD the two *biggest* bursts were a giveaway (126 of 163 messages were the
  keyword "gta") and a poll ("marathon"/"end", 620 of 667). If one or two **short** (≤ 2 words) non-reaction phrases
  make up ≥ 50% of a burst, chat was told what to type: the spike is skipped and `spikes.skip_reason` says why.
  Reaction spam ("W", "LMAO", "NOOO", "GG") and copypasta sentences don't count, so real hype passes.
- Idempotent: `spikes` is unique on `(session, start_bucket)`; a growing burst updates its own row while open.

## 5. Past streams / VOD mode (`app/vod.py`)
Streamers stop streaming daily once launch hype fades, but every stream stays on their channel with its chat replay.
1. **Find** (`vod_scan`, every 30 min): the newest 6 entries of each channel's Live tab via yt-dlp (free), then one
   batched `videos.list` (1 unit) for start/end times. Queued if finished, the title mentions GTA 6, it ended in the
   last 72 h and is ≥ 20 min long; otherwise recorded as `skipped` with the reason, so it's never asked about again.
   Pasted links (dashboard) skip these filters but the channel must be added.
2. **Read the chat replay** (no quota): 6.2 h of Lacy's chat took 56 s and 11 MB; a 100k-message stream ~1–2 min.
   Messages go into the same `chat_messages` table, then the **same buckets and spike detector**.
   - **Clock:** replay second 0 is the stream's `actualStartTime` (measured 0.3 s apart, spread 0.1 s), so
     `VOD second = chat time − offset_utc`, with the offset measured from the replay itself.
   - **Chat delay:** a manual channel setting wins; else what was measured when the channel was recorded live
     (Lacy 39.8 s); else `VOD_CHAT_DELAY_SECONDS` (35).
3. **Plan:** the `VOD_MAX_CLIPS` (6) strongest spikes become clips (`source_kind = 'vod'`, window in VOD seconds).
   Skipped: moments already clipped live, bursts under `VOD_MIN_PEAK_CHATTERS` (10), giveaways/polls, and chat from
   the waiting room before the stream started.
4. **Footage:** only the window ±3 s is downloaded as an **HLS** section: ~55 s and ~11 MB for 46 s of 720p60.
   Never the 10-hour file (that would be ~10 GB). DASH sections stall (5+ min for 2 min of video), so `VOD_FORMAT`
   prefers HLS. Facecam detection runs on the first sections.
5. **Clean up:** once a stream's clips have footage, its raw chat is deleted (each clip keeps a chat sample).
- One stream at a time, newest first (pasted links first). Pauses while anything is recording.
- **No chat replay:** some streamers turn it off (3 of 16 recent streams). Retried hourly 6 times (it can appear
  after processing), then `no_chat`. An audio-based fallback is an open idea.
- yt-dlp needs **Deno** and the `yt-dlp-ejs` package to solve YouTube's JS challenges.

## 6. Clips (`app/clips/`, `app/captions/`, `app/qc/`)
**Review first, edit later.** The AI review runs on the raw footage, so rejected clips never cost a render.

| Status | Meaning | Worker |
|---|---|---|
| `pending` | planned from a spike | |
| `cutting` → `cut` | footage secured: live = byte-concatenated `.ts` pieces (lossless, stream PTS kept); VOD = downloaded section | `ClipWorker` |
| `captioning` → `transcribed` | Whisper `small` transcript with word timestamps | `CaptionWorker.run_all` |
| `evaluating` → `passed` / `rejected` | objective checks + Ollama review | `QcWorker` |
| `rendering` → `approved` | 9:16 render with layout + captions in one pass | `CaptionWorker.render_all` |
| `published` | on YouTube | `PublishQueue` |
| `failed` | 3 failed attempts at a step (outages don't count) | |

- **Time base:** `window_*_pts` and `source_start_pts` share one clock: stream PTS for live clips, VOD seconds for
  past-stream clips. Every stage computes `offset = window_start_pts − source_start_pts`, so stages don't care which.
- **Transcript:** `WHISPER_BACKEND=openai` (official openai-whisper, weights from OpenAI's CDN) by default, because
  Hugging Face downloads were rate-limited/blocked here; `faster` is ~2–3× faster if Hugging Face works. Measured
  30–160 s per 40 s clip on CPU.
- **Nudity check (`qc/nudity.py`):** before the AI review, NudeNet scans the whole raw window at 2 fps, on the full
  frame and four overlapping 60% quarters (a person in a wide shot is too small at the model's 320 px; a quarter makes
  them ~2.8× bigger). A clip is rejected when exposed genitals, buttocks, breasts or anus score ≥ `QC_NUDITY_THRESHOLD`
  (0.4) on **≥ 3 frames** (1.5 s), or any single hit is ≥ 0.75 ("nudity: exposed buttocks at 0:26 (0.66), on 11
  frames"). Built after a TGG story cutscene with a nude character at the end of a clip was approved by the AI (6
  frames missed it) and caught by hand. Measured on 30 real clips: that clip hit 11 frames (max 0.66); every false alarm
  (a shoulder in a tank top, a tattooed arm, a cat thumbnail, a face on a pink pillow) hit exactly one frame
  (max 0.52), so the frame count separates them where a higher threshold could not. The AI is also asked a `nudity` question as a second layer. Clips approved
  before the scan existed are scanned before upload (`check_unscanned`), and the upload queue only takes clips with
  `nudity_checked_at` set. ~10–25 s per clip on CPU.
- **AI review (`qc/`):** hard rejects first (black > 30%, frozen > 50%, no audio), measured on the raw window. Then
  `gemma4:e2b` gets 6 frames + transcript + chat and scores excitement, reaction, gameplay, standalone, picture quality,
  clean start/end (0–10), plus a `reject` flag. Final = 0.6 × AI + 0.4 × objective (chat strength, speech); pass at
  `QC_MIN_SCORE` (6.0). ~2 min per clip. It also reports *GTA on screen* and *funny*: information only. A rule that
  rejected clips without GTA on screen unless very funny was tried and removed: it rejected good clips (pause menu,
  post-credits scene).
- **Operator note:** "Tell the AI what's on screen" (`clips.review_note`) is put into the review and title prompts as
  trusted context, then the clip is reviewed again.
- **Render (`clips/render.py`):** 1080×1920, −14 LUFS. `split` = facecam band on top, gameplay below (when a webcam
  rectangle is known); `blur` = full frame over a blurred fill. Facecam detection (`clips/facecam.py`): YuNet face
  detector finds a stable face, then edges/motion find the overlay's box; per stream, automatic, overridable per channel.
- **Captions (`captions/ass.py`):** `.ass` with word-highlight karaoke, 3 words per line, uppercase, plus an
  `@channel` credit; burned in by libass during the single render.
- **Files:** `data/clips/<video_id>/clipNNNNN/`. Deleted `CLIP_PURGE_GRACE_MINUTES` (60) after upload and
  `CLIP_PURGE_REJECTED_MINUTES` (360) after rejection; transcripts, scores and links stay in the database.

## 7. Publishing (`app/publish/`)
- **Gate:** only `approved` clips from channels with `permission_confirmed = 1` (*Upload allowed*) are queued.
- **Metadata (`metadata.py`):** the local model writes 4 candidate titles in different hook styles from the facts
  (stream title, transcript, informative chat messages, loudest reactions, the operator note); a deterministic scorer
  picks one (specific nouns, length, no banned hype words, no profanity). Description = hook + question + credit line
  with a link to the exact moment; ≤ 5 hashtags incl. `#Shorts`. Template fallback if the model fails.
- **Posting strategy (`queue.py: PostingStrategy`):** one post per slot at `PUBLISH_SLOTS` on `PUBLISH_SLOTS_TIMEZONE`
  (12:00, 14:30, 17:00, 19:00, 21:00, 23:00 US Eastern). Pick = `score − 1/day since the moment − 1 if same streamer
  as the last post`, ≤ 2 posts per stream per day. **Breaking lane:** a live clip scoring ≥ 8 within 2 h of the moment
  posts at once. Queued clips never expire (owner's choice; `PUBLISH_MAX_AGE_HOURS` > 0 turns expiry on). Always: ≤ 6 per Pacific day, ≥ 30 min apart. A slot missed while the
  laptop slept posts once on wake (no burst).
- **Upload (`youtube.py`):** resumable `videos.insert`, 1 quota unit from a separate allowance of 100/day. Errors are
  classified: `network` (wait 5 min, no attempt used), `quota` (wait for the Pacific reset), `auth` (sign in again),
  `fatal`, `retry` (5/15/60 min, 3 attempts). 10 min after upload the real privacy is checked: unaudited Google
  projects may have API uploads locked to private (ours came out public in November 2026); a lock triggers an alert.
- **Sign-in:** OAuth desktop client; in Testing mode the token lasts 7 days (`python -m app.publish.auth`).

## 8. Alerts (`app/notify/`)
Every 30 s the notifier scans the database and sends each event once (`notifications` table): streamer live, clip
approved (with what happens next), Short uploaded (with *Open in Studio* when it's private), and problems (upload
failed, locked to private, sign-in expiring, disk low). ntfy.sh JSON publish to a private random topic
(`data/ntfy.json`); Telegram as an alternative (this ISP blocks Telegram's bot API servers, which is why ntfy exists).

## 9. Data model (`app/db.py`)
Append-only migrations tracked by `PRAGMA user_version` (16 so far). Main tables:

| Table | Holds |
|---|---|
| `channels` | tracked channels and their switches |
| `videos_seen` | every video ID seen (so finished ones aren't re-queried) |
| `live_sessions` | one row per stream (live, ended, or created for a past stream) |
| `recordings`, `segments`, `time_anchors` | recorder runs, 60 s pieces, PTS↔UTC pairs |
| `chat_messages`, `chat_buckets`, `chat_runs` | raw chat (temporary), 10 s aggregates, collector runs |
| `spikes` | detected bursts (`source` live/vod, `skip_reason`) |
| `clips`, `transcripts`, `evaluations` | the clip lifecycle, words, AI verdicts |
| `publish_queue`, `publish_attempts` | uploads and their history |
| `vod_jobs` | past streams found and what happened to each |
| `quota_usage`, `errors`, `notifications` | quota ledger, problems shown on the dashboard, alerts sent |

## 10. Dashboard (`app/static/`)
Plain HTML/CSS/JS, no build step, served by the app. Polls `/api/overview` every 5 s (plus `/api/clips`,
`/api/publish`). The HTTP API is browsable at http://127.0.0.1:8000/docs.

## Measured numbers, in one place
| What | Measured |
|---|---|
| Chat delay, Lacy (normal latency) | 39.8 s predicted, ~39 s observed |
| Chat replay offset vs actualStartTime | 0.3 s (spread 0.1 s) |
| 6.2 h chat replay download | 56 s, 11 MB, 7,823 messages |
| 46 s HLS section, 720p60 | ~55 s, ~11 MB (DASH: stalled > 5 min) |
| Streams with chat replay | 13 of 16 recent ones across the four channels |
| yt-dlp chat coverage (Top chat) | ~71% of messages |
| Official chat cost | ~1,780 units per stream-hour |
| Whisper small, 40 s clip, CPU | 30–160 s |
| AI review (gemma4:e2b, 6 frames) | ~2 min |
| Nudity scan (NudeNet, 80 frames × 5 views) | ~10–25 s |
| Title generation | 70–90 s |
| Ctrl+C to process gone (`python -m app`) | 0.2 s, even mid-download |
