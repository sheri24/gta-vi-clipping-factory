from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    youtube_api_key: str = ""
    data_dir: Path = Path("data")
    poll_interval_seconds: int = 60
    live_page_probe: bool = True
    quota_daily_budget: int = 9000

    # Recording (Phase 3)
    record_quality: str = "720p60,720p,best"   # Streamlink quality fallback list
    segment_seconds: int = 60
    buffer_minutes: int = 30                    # rolling window kept while live
    end_cleanup_grace_minutes: int = 10         # wait after stream end before wiping footage
    min_free_gb: float = 10.0                   # don't start new recordings below this
    anchor_interval_minutes: int = 10           # how often to re-sample the PTS→UTC anchor
    streamlink_bin: str = ".venv/Scripts/streamlink.exe"
    ytdlp_bin: str = ".venv/Scripts/yt-dlp.exe"   # pip-installed, kept current (winget copy was stale)

    # Live chat (Phase 4). Tried in order; on repeated failure the collector moves to the next.
    chat_sources: str = "ytdlp,official"
    chat_bucket_seconds: int = 10

    # Spike detection (Phase 5)
    spike_baseline_minutes: float = 5      # trailing window for the rolling median
    spike_future_minutes: float = 3        # baseline also looks this far AFTER the moment
    spike_guard_seconds: int = 60          # ...skipping this much either side of the moment itself
    spike_min_history_minutes: float = 2   # don't judge until we know what "normal" is
    spike_z: float = 3.5                   # robust z-score threshold
    spike_ratio: float = 1.6               # and at least this many × the baseline
    spike_min_authors: int = 5             # and at least this many distinct chatters per bucket
    spike_merge_gap_seconds: int = 20      # bursts closer than this are one moment
    clip_pre_seconds: float = 15
    clip_post_seconds: float = 25

    # Clip extraction (Phase 6)
    clip_purge_after_publish: bool = True    # delete a clip's local files once it's on YouTube
    clip_purge_grace_minutes: int = 60        # ...this long after upload (0 = right away)
    clip_purge_rejected: bool = True          # delete a rejected clip's local files too
    clip_purge_rejected_minutes: int = 360    # ...this long after rejection: time to look and overrule
    clip_min_overlap_seconds: float = 10    # a spike overlapping an existing clip this much reuses it
    render_encoder: str = "libx264"         # or h264_qsv (Intel Quick Sync)
    render_crf: int = 20                    # x264 quality (lower = better/larger); QSV uses it as global_quality
    loudness_lufs: float = -14              # Shorts/TikTok loudness target

    # Past streams (VOD mode): clip uploaded streams from their chat replay while nobody is live
    vod_enabled: bool = True
    vod_scan_minutes: int = 30              # how often to look for newly finished streams (1 quota unit a scan)
    vod_streams_per_channel: int = 6        # newest entries of each channel's Live tab to consider
    vod_max_age_hours: float = 72           # older streams are skipped: everyone has clipped them by then
    vod_min_minutes: float = 20             # shorter streams are skipped
    vod_title_keywords: str = "gta 6,gta vi,gta6,gtavi,grand theft auto"   # empty = any title
    vod_max_clips: int = 6                  # strongest chat spikes per stream (uploads are capped per day anyway)
    vod_min_peak_chatters: int = 10         # weaker bursts aren't worth a clip from a past stream
    vod_chat_delay_seconds: float = 35      # chat lag when the channel was never measured live (Lacy: 39.8 s)
    vod_chat_retry_minutes: int = 60        # chat replay can appear hours after a stream ends
    vod_chat_retries: int = 6
    vod_pause_while_live: bool = True       # a live recording gets the bandwidth and CPU
    vod_format: str = ("bv*[height<=720][protocol^=m3u8]+ba[protocol^=m3u8]/b[height<=720][protocol^=m3u8]"
                       "/bv*[height<=720]+ba/b")   # HLS first: a section downloads in ~1 min, DASH can stall

    # Transcription + captions (Phase 7)
    whisper_backend: str = "openai"         # openai (weights from OpenAI's CDN) | faster (weights from Hugging Face)
    whisper_model: str = "small"            # multilingual; "base" is ~2x faster, less accurate
    whisper_threads: int = 8
    whisper_beam_size: int = 1              # 1 = greedy (fast); 5 = beam search (slower, slightly more accurate)
    hf_token: str = ""                      # free Hugging Face read token: avoids anonymous 429s on model download
    caption_font: str = "Arial Black"
    caption_size: int = 84                  # px on the 1080x1920 canvas
    caption_color: str = "FFFFFF"           # RGB hex
    caption_highlight: str = "FFE11A"       # active word
    caption_outline: int = 7
    caption_margin_v: int = 470             # px from the bottom (clears the Shorts UI) - blur layout
    caption_margin_v_split: int = 1180      # split layout: on the seam under the 640 px camera band
    caption_words_per_line: int = 3
    caption_uppercase: bool = True
    credit_enabled: bool = True             # "@channel" tag on screen

    # Quality control (Phase 8)
    ollama_url: str = "http://127.0.0.1:11434"
    qc_model: str = "gemma4:e2b"            # must accept images
    qc_frames: int = 6
    qc_min_score: float = 6.0               # 0-10; approved at or above
    qc_llm_weight: float = 0.6              # final = w*llm + (1-w)*objective
    qc_nudity_threshold: float = 0.4        # NudeNet confidence that rejects a clip (exposed genitals/buttocks/breasts)

    # Publishing (Phase 9)
    publish_enabled: bool = False           # master switch; off until OAuth is set up
    telegram_bot_token: str = ""            # from @BotFather; then run: python -m app.notify.telegram
    telegram_chat_id: str = ""              # filled automatically by the setup command (data/telegram.json)
    ntfy_topic: str = ""                    # filled by: python -m app.notify.ntfy (data/ntfy.json)
    ntfy_server: str = "https://ntfy.sh"
    notify_live: bool = True                # also alert when a streamer goes live
    metadata_model: str = ""                # text model for titles; empty = same as QC_MODEL
    publish_privacy: str = "private"        # private | unlisted | public (public needs an audited API project)
    publish_interval_minutes: int = 30
    publish_max_per_day: int = 6            # per Pacific-time day; new channels posting 48/day look like spam
    # Posting strategy: clips are made around the clock, but go out in slots over the audience's evening.
    publish_slots: str = "12:00,14:30,17:00,19:00,21:00,23:00"   # empty = post as soon as ready
    publish_slots_timezone: str = "America/New_York"             # the audience's clock
    publish_breaking_score: float = 8.0     # a live clip this good, this fresh, posts at once
    publish_breaking_minutes: int = 120
    publish_max_age_hours: float = 96       # older moments are dropped instead of posted
    publish_max_per_stream_per_day: int = 2
    publish_category_id: str = "20"         # YouTube "Gaming"
    publish_base_tags: str = "gta 6,gta vi,gta 6 gameplay,grand theft auto 6,gaming,shorts"

    @property
    def client_secret_path(self) -> Path:
        return self.data_dir / "client_secret.json"

    @property
    def youtube_token_path(self) -> Path:
        return self.data_dir / "youtube_token.json"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def chat_dir(self) -> Path:
        return self.data_dir / "chat"

    @property
    def vod_dir(self) -> Path:
        return self.data_dir / "vod"
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    @field_validator("streamlink_bin", "ytdlp_bin", "ffmpeg_bin", "ffprobe_bin")
    @classmethod
    def _absolute_tool_path(cls, v: str) -> str:
        """'.venv/Scripts/x.exe' → absolute. Windows CreateProcess won't reliably launch a relative
        path with forward slashes (it depends on how the server was started); bare names use PATH."""
        p = Path(v)
        if p.is_absolute() or len(p.parts) == 1:
            return v
        return str((PROJECT_ROOT / p).resolve())

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipper.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
