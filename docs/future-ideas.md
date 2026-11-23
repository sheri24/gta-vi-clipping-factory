# Future ideas

Ideas raised while building, parked here to review later. Items marked **built** are done; the rest are not.
Rough size: **S** = an afternoon, **M** = a few days, **L** = a project of its own.

## Shorts packaging (next up after the MVP)

- **Catchier titles, descriptions and hashtags** (M). Write the hook from the transcript plus the
  loudest chat reactions, not just the vision model's summary (gemma4:e2b summaries were vague, so
  titles came out generic). Try gemma4:e4b or a larger local model only for metadata, add a few
  example titles in the house style, and generate 3 candidates then pick the best with a scoring pass.
- **Custom thumbnails** (M). Pick the most expressive facecam frame (YuNet face + mouth/eye openness)
  and a sharp gameplay frame, compose a 1080×1920 image with short hook text, upload with
  `thumbnails.set` after `videos.insert`. Needs the channel to be phone-verified (YouTube Studio →
  Settings → Channel → Feature eligibility). Write the title and thumbnail text together as one hook.
- **Better clip summaries for QC** (S). Feed the AI judge more frames around the spike peak and the
  chat sample, so the summary names what happened ("Jason gets shot") instead of "chaotic moments".

## Pipeline quality

- ~~Clean up rejected clips~~: **built** (deleted `CLIP_PURGE_REJECTED_MINUTES` after rejection).
- ~~Review before editing~~: **built** (the AI judges raw footage; only passed clips are rendered).
- **Layout tuning** (S–M). Per-channel crop of the gameplay band, facecam padding, caption position.
- **Faster renders** (S). Try Intel QSV (`RENDER_ENCODER=h264_qsv`) on this laptop.
- **Smarter clip boundaries** (M, **requested by the owner**). Every clip is a fixed 40 s (15 s before / 25 s
  after the moment). Trim to the main part instead: start at the sentence before the moment (transcript word
  timings), peak at the loudest second (audio), end a couple of seconds after the reaction or sentence ends;
  ~12–30 s. Do it just before the final render and shift the caption word timings by the trim.

## Ideas from a similar tool (NoCodeHack "gta6-clipper" video, seen 2026-11-23)

- **Face tracking** (M). Follow the streamer's face so the camera band stays centred when they move,
  instead of a fixed facecam rectangle.
- **Your own voice line** (S–M). Record one line, mix it over the clip right after the climax with the game
  audio ducked. Commentary makes a Short "transformed", which helps with YouTube's reused-content rules.
- ~~Spread posting through the day~~: **built** (posting strategy: slots, breaking lane, expiry).
- **Keyboard review** (S). A = approve, X = reject, arrows to move through clips on the dashboard.
- **Edit a clip by typing** (M). "End 2 seconds earlier" → a new version next to the original.
- **Twitch/Kick chat** (M). xQc and Kai stream mainly on Twitch, where chat is far bigger than their YouTube
  chat (89 → 300 messages per 10 s quoted for xQc vs 1.3 chatters per 10 s measured on YouTube). Twitch chat
  can be read anonymously for free (IRC).

## Publishing

- **YouTube API Services audit** (S to write, weeks to approve). Lifts the lock that keeps API uploads
  private. Needs live URLs (homepage, privacy policy, terms), written permission from each streamer,
  and an honest description of the recording step. Started on 2026-11-23 and parked until there's a site.
  Drafts of the form answers (organisation's work, audience, content owner N/A, client name) are in the
  chat history.
- **Move the Google OAuth app to production** (S). Needs a privacy policy and terms page (free on
  GitHub Pages or Google Sites) and branding info. Removes the weekly re-sign-in of Testing mode.
- **Other platforms** (M each). TikTok, Instagram Reels, Facebook Reels. Each has its own API
  approval process; some don't allow fully automatic posting for new apps.
- **Optional Composio adapter** (S). Only if a hosted connector ever becomes useful.

## Beyond live streams

- ~~Past streams (VODs)~~: **built** (VOD mode, chat replay → spikes → section download). Still open:
  - **Streams without chat replay** (M). 3 of 16 recent streams had it off (including Speed's 2026-11-22).
    Fall back to audio loudness peaks (download the audio only, ~350 MB for 6 h, ffmpeg `ebur128`) plus
    timestamps in top comments (45 of 418 top comments on Speed's VOD had one).
  - **Regular uploads** (M). Edited videos have no chat; same fallback signals.
  - **GTA 6 videos from channels you don't track** (S–M). `search.list` now costs 1 unit from its own allowance of
    100 calls a day (quota table checked 2026-11-23), so discovery is cheap; clips could only be uploaded once that creator agrees.
  - **YouTube's "Most replayed" graph** (S) as an extra signal once it appears (none of the VODs checked had one yet).
- **Long-form compilations** (L). For example "GTA 6 top 10 fails": rank the best approved clips of a
  week, stitch them with intros and a source credit on screen for each clip.
- **Researched explainer videos** (L). For example "Deconstructing every secret detail hidden in the
  alternate endings of GTA 6":
  1. Research with a local LLM over wiki pages, Reddit threads and your own notes. Small models
     invent facts, so the script needs a human check before publishing.
  2. Write the script with a larger local model, in sections with on-screen text cues.
  3. Voiceover with free local text-to-speech (Kokoro-82M or Piper; check each voice's licence for
     monetised use).
  4. Footage: your own gameplay recordings are safest; streamer footage only with permission.
     Match footage to each script line using Whisper transcripts plus an open vision-search model.
  5. Editing with FFmpeg + MoviePy: zooms, kinetic captions, transitions, CC0 sound effects,
     YouTube Audio Library music.
  Risks: YouTube's inauthentic/reused-content rules can demonetise mass-produced AI videos (real
  commentary and original footage help); check Rockstar's video policy; renders are slow on a 15 W CPU.
