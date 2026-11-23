# Narrated GTA 6 Shorts (prototype)

A second kind of Short that doesn't need anyone to be live: a narrator tells 5 GTA 6 secrets over matching gameplay,
with captions, background music, transitions and the channel logo. Everything runs locally and costs $0. It is kept
apart from the clip factory (`app/`); it only reads `app/captions/ass.py` and, read-only, the factory's database for
the creator's own past streams.

## Pipeline

| Step | File | What it does |
|---|---|---|
| Research | `research.py` | Facts pulled from "GTA 6 secrets" videos (transcribed with Whisper) are merged across videos and ranked: secrets several videos mention come first. |
| Script | `script.py` | A local Ollama model (`gemma4:e2b`) writes it in small steps: rate the facts, write one line per fact, check each line against its fact (names, places and numbers kept), then write a hook and a closing question. |
| Voice | `voice.py` | Kokoro TTS (`am_puck`), with a slightly faster hook and a slightly slower question. |
| Footage | `broll.py`, `visual_index.py` | Shows the exact moment a research video demonstrates the secret when its word timings are known. Otherwise it searches YouTube storyboards with CLIP and checks the best candidates frame by frame; other GTA games, title cards and webcams are rejected. |
| Edit | `edit.py` | 9:16 crop with a slow push-in, xfade transitions with a whoosh, music ducked under the voice, word captions, and a logo in the top-left corner. |

## Use

```powershell
narrator\.venv\Scripts\python -m narrator.make_short data\narrator\research\facts_ranked.json --facts-count 14 `
    --music "data\narrator\music\Background Audio.mp3" --music-start 19
# change only the edit (music level, logo) of a finished Short, about 1 minute:
narrator\.venv\Scripts\python -m narrator.make_short --reedit data\narrator\out\<name>.json `
    --music "data\narrator\music\Background Audio.mp3" --music-start 19 --music-volume 0.45
```

Everything it makes stays in `data/narrator/` (git-ignored): research, caches, the logo (`brand/logo.png`), music and
the output.

Measured: about 27 min per Short on CPU (script ~9, footage ~15, edit ~2). Building the visual index the first time
takes ~1.5 h (28 videos, ~36 MB).

## Setup and limits
- Install with [requirements.txt](requirements.txt) into its own venv. The Kokoro model files come from the
  kokoro-onnx GitHub release and the CLIP weights from OpenAI's CDN, because Hugging Face is blocked on the owner's
  network.
- Licenses: Kokoro's phonemizer dependency is GPL-3.0. The footage belongs to other creators: credit the sources
  listed in `<name>.json`.
- Nothing here uploads. Posting a narrated Short is a manual decision.
