"""FastAPI app. Start it with:  .\\.venv\\Scripts\\python -m app   (see app/__main__.py)

Interactive API (add/remove channels, poll now): http://127.0.0.1:8000/docs
"""

import json
import subprocess
import os
import threading
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import channels as ch
from app import dashboard
from app.logsafe import SecretFilter, scrub_file, tail
from app.private_files import secure_existing
from app.config import get_settings
from app.db import Database
from app.chat.collector import ChatManager
from app.chat.official_source import OfficialChatSource
from app.chat.store import ChatStore
from app.chat.ytdlp_source import YtDlpChatSource
from app.monitor import Monitor
from app.notify.notifier import Notifier, Senders
from app.notify.ntfy import Ntfy, saved_topic
from app.notify.telegram import Telegram, saved_chat_id
from app.captions.transcribe import make_transcriber
from app.captions.worker import CaptionWorker, caption_style
from app.clips.facecam import YuNetFaces, detect_for_sessions
from app.clips.pipeline import ClipWorker, plan_clips, render_options_factory
from app.procs import contain_children
from app.publish.metadata import MetadataWriter, clip_context
from app.publish.queue import PostingStrategy, PublishQueue, blocked_by_permission, enqueue_approved
from app.publish.youtube import YouTubePublisher, sign_in_status
from app.qc.llm import OllamaJudge
from app.qc.worker import QcWorker
from app.spikes.detector import Params, SpikeDetector
from app.recording.recorder import RecorderManager, disk_free_gb
from app.recording.retention import (cleanup_ended_sessions, enforce_rolling_buffer, purge_published_clips,
                                     purge_rejected_clips, storage_summary)
from app.youtube.api import QuotaExceeded, QuotaLedger, YouTubeApi, YouTubeApiError
from app.youtube.web import YouTubeWeb
from app.vod import VodPolicy, VodScanner, VodTools, VodWorker, facecam_probe


def build_services(settings) -> dict:
    db = Database(settings.db_path)
    ledger = QuotaLedger(db, settings.quota_daily_budget)
    api = YouTubeApi(settings.youtube_api_key, ledger)
    web = YouTubeWeb()
    monitor = Monitor(db, api, web, live_page_probe=settings.live_page_probe)
    recorders = RecorderManager(db, settings)
    chat_store = ChatStore(db, settings.chat_bucket_seconds)
    chat = ChatManager(db, chat_store, lambda: build_chat_sources(settings, ledger))
    for listener in (recorders, chat):
        monitor.on_started.append(listener.on_session_started)
        monitor.on_ended.append(listener.on_session_ended)
    spikes = SpikeDetector(db, spike_params(settings), settings.clip_pre_seconds, settings.clip_post_seconds)
    # after chat's on_ended has written the final buckets
    monitor.on_ended.append(lambda session: spikes.run(session["id"], final=True))
    render_opts = render_options_factory(db, settings)
    faces = YuNetFaces(settings.models_dir)
    vod_tools = VodTools(settings.ytdlp_bin, settings.ffmpeg_bin, settings.vod_format)
    vod_policy = VodPolicy.from_settings(settings)
    paused = lambda: vod_pause_reason(settings, db, recorders)   # noqa: E731
    clip_worker = ClipWorker(db, settings.clips_dir, settings.ffprobe_bin,
                             vod_fetch=vod_tools.download_section, vod_paused=lambda: paused() is not None,
                             after_vod_fetch=facecam_probe(db, faces, settings.ffmpeg_bin))
    vod_scanner = VodScanner(db, api, vod_tools, vod_policy)
    vod_worker = VodWorker(db, vod_tools, chat_store, spikes, vod_policy, settings.vod_dir, paused=paused)
    transcriber = make_transcriber(settings)
    caption_worker = CaptionWorker(db, transcriber, render_opts, caption_style(settings),
                                   settings.credit_enabled, settings.ffmpeg_bin)
    caption_worker.split_margin_v = settings.caption_margin_v_split
    qc_worker = QcWorker(db, OllamaJudge(settings.ollama_url, settings.qc_model), settings.qc_frames,
                         settings.qc_min_score, settings.qc_llm_weight, settings.ffmpeg_bin)
    writer = MetadataWriter(settings.ollama_url, settings.metadata_model or settings.qc_model,
                            [t.strip() for t in settings.publish_base_tags.split(",") if t.strip()])
    publish_queue = PublishQueue(db, YouTubePublisher(settings.youtube_token_path, settings.publish_category_id),
                                 writer, enabled=settings.publish_enabled, privacy=settings.publish_privacy,
                                 interval_minutes=settings.publish_interval_minutes,
                                 max_per_day=settings.publish_max_per_day,
                                 strategy=PostingStrategy.from_settings(settings))
    telegram = Telegram(settings.telegram_bot_token, settings.telegram_chat_id or saved_chat_id(settings.data_dir))
    ntfy = Ntfy(settings.ntfy_topic or saved_topic(settings.data_dir), settings.ntfy_server)
    notifier = Notifier(db, Senders([telegram, ntfy]), settings, publish_queue, notify_live=settings.notify_live)
    return {"notifier": notifier, "db": db, "ledger": ledger, "api": api, "web": web, "monitor": monitor,
            "recorders": recorders, "chat": chat, "chat_store": chat_store, "spikes": spikes,
            "clip_worker": clip_worker, "caption_worker": caption_worker, "qc_worker": qc_worker,
            "publish_queue": publish_queue, "metadata_writer": writer, "faces": faces,
            "vod_scanner": vod_scanner, "vod_worker": vod_worker, "vod_paused": paused, "settings": settings}


def vod_pause_reason(settings, db: Database, recorders) -> str | None:
    """Why past-stream work waits right now, or None. Live recording comes first (bandwidth, CPU)."""
    if settings.vod_pause_while_live:
        live = [r.session for r in recorders.recorders.values() if r.is_alive()]
        if live:
            row = db.one("SELECT title FROM channels WHERE channel_id = ?", (live[0]["channel_id"],))
            return f"{row['title'] if row else 'A streamer'} is live, so the recording comes first"
    if disk_free_gb(settings.recordings_dir) < settings.min_free_gb:
        return f"less than {settings.min_free_gb:g} GB free on disk"
    return None


def spike_params(s) -> Params:
    per_min = 60 // s.chat_bucket_seconds
    return Params(bucket_seconds=s.chat_bucket_seconds,
                  baseline_buckets=int(s.spike_baseline_minutes * per_min),
                  future_buckets=int(s.spike_future_minutes * per_min),
                  guard_buckets=s.spike_guard_seconds // s.chat_bucket_seconds,
                  min_history_buckets=int(s.spike_min_history_minutes * per_min),
                  z_min=s.spike_z, ratio_min=s.spike_ratio, min_authors=s.spike_min_authors,
                  merge_gap_buckets=max(0, s.spike_merge_gap_seconds // s.chat_bucket_seconds))


def run_clip_pipeline(services: dict) -> None:
    """Fast path: spikes → cut clips. Kept separate so footage is secured even while the slow
    stages below are busy (e.g. a first-run model download blocked cutting for 15 min)."""
    detect_for_sessions(services["db"], services["faces"], services["settings"].ffmpeg_bin)
    plan_clips(services["db"], services["settings"].clip_min_overlap_seconds)
    services["clip_worker"].run_all()


def run_post_pipeline(services: dict) -> None:
    """Slow path: transcript (Whisper) → AI review (Ollama) → render only what passed."""
    services["caption_worker"].run_all()
    services["qc_worker"].run_all()
    services["caption_worker"].render_all()


def run_publisher(services: dict) -> None:
    services["publish_queue"].run_once()


def run_chat_analysis(services: dict) -> None:
    services["chat"].aggregate_all()
    for c in list(services["chat"].collectors.values()):
        if c.is_alive():
            services["spikes"].run(c.session["id"])


def build_chat_sources(settings, ledger) -> list:
    available = {
        "ytdlp": lambda: YtDlpChatSource(settings.ytdlp_bin, settings.chat_dir),
        "official": lambda: OfficialChatSource(settings.youtube_api_key, ledger),
    }
    return [available[name.strip()]() for name in settings.chat_sources.split(",") if name.strip() in available]


def run_retention(services: dict) -> None:
    s, db = services["settings"], services["db"]
    freed = enforce_rolling_buffer(db, s.buffer_minutes)
    freed += cleanup_ended_sessions(db, s.end_cleanup_grace_minutes,
                                    services["recorders"].is_recording, s.recordings_dir,
                                    on_cleaned=services["chat_store"].delete_session_messages)
    if s.clip_purge_after_publish:
        freed += purge_published_clips(db, s.clips_dir, s.clip_purge_grace_minutes)
    if s.clip_purge_rejected:
        freed += purge_rejected_clips(db, s.clips_dir, s.clip_purge_rejected_minutes)
    if freed:
        logging.getLogger("retention").info("freed %.1f MB", freed / 1024**2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    scrub_file(settings.log_dir / "app.log")      # older runs logged request URLs with ?key=
    # secrets saved before this protection existed (or saved by hand, like the OAuth client file)
    secure_existing([settings.youtube_token_path, settings.client_secret_path,
                     settings.data_dir / "ntfy.json", settings.data_dir / "telegram.json", Path(".env")])
    handlers = [logging.StreamHandler(), logging.FileHandler(settings.log_dir / "app.log", encoding="utf-8")]
    for h in handlers:
        h.addFilter(SecretFilter())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)   # one line per request is noise
    if not contain_children():
        logging.getLogger(__name__).warning("child processes may outlive a crash of this app")
    services = build_services(settings)
    services["db"].mark_interrupted()
    app.state.services = services

    scheduler = BackgroundScheduler()
    # max_instances=1 + coalesce: a slow poll never overlaps the next one.
    scheduler.add_job(services["monitor"].poll, "interval", seconds=settings.poll_interval_seconds,
                      id="monitor", max_instances=1, coalesce=True, next_run_time=datetime.now())
    scheduler.add_job(services["recorders"].sample_anchors, "interval",
                      minutes=settings.anchor_interval_minutes, id="anchors", max_instances=1, coalesce=True)
    scheduler.add_job(run_retention, "interval", seconds=60, args=[services], id="retention",
                      max_instances=1, coalesce=True)
    scheduler.add_job(run_chat_analysis, "interval", seconds=settings.chat_bucket_seconds, args=[services],
                      id="chat_analysis", max_instances=1, coalesce=True)
    # one heavy job at a time: max_instances=1 keeps renders sequential on the 15 W CPU
    scheduler.add_job(run_clip_pipeline, "interval", seconds=15, args=[services], id="clips",
                      max_instances=1, coalesce=True)
    scheduler.add_job(run_post_pipeline, "interval", seconds=20, args=[services], id="post",
                      max_instances=1, coalesce=True)
    scheduler.add_job(run_publisher, "interval", seconds=60, args=[services], id="publish",
                      max_instances=1, coalesce=True)
    scheduler.add_job(services["notifier"].run, "interval", seconds=30, id="notify",
                      max_instances=1, coalesce=True)
    if settings.vod_enabled:
        scheduler.add_job(services["vod_scanner"].scan, "interval", minutes=settings.vod_scan_minutes, id="vod_scan",
                          max_instances=1, coalesce=True, next_run_time=datetime.now() + timedelta(seconds=30))
        scheduler.add_job(services["vod_worker"].run_once, "interval", seconds=60, id="vod",
                          max_instances=1, coalesce=True)
    if not services["notifier"].sender.enabled:
        logging.getLogger(__name__).info("phone alerts off: set up ntfy (app/notify/ntfy.py) or Telegram (app/notify/telegram.py)")
    services["recorders"].resume()
    services["chat"].resume()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)
    services["chat"].stop_all()
    services["recorders"].stop_all()  # lets ffmpeg finalise open segments
    # A job already running (Whisper, the AI review, a chat download) can take minutes, and Python waits
    # for it before exiting, so Ctrl+C looked stuck. Every step resumes on the next start (mark_interrupted),
    # so after a short grace period exit anyway; Windows' kill-on-close job ends yt-dlp/ffmpeg children with us.
    threading.Timer(SHUTDOWN_GRACE_SECONDS, _force_exit).start()
    services["db"].close()


SHUTDOWN_GRACE_SECONDS = 5


def _force_exit() -> None:
    logging.getLogger(__name__).info("background work still running at shutdown; exiting (it resumes next start)")
    logging.shutdown()
    os._exit(0)


app = FastAPI(title="GTA VI Clipping Factory", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def svc(request: Request) -> dict:
    return request.app.state.services


@app.get("/", include_in_schema=False)
def dashboard_page():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/overview")
def get_overview(request: Request):
    return dashboard.overview(svc(request))


@app.get("/api/logs")
def get_logs(request: Request, lines: int = 200):
    return {"lines": tail(svc(request)["settings"].log_dir / "app.log", max(1, min(lines, 2000)))}


class ClipAction(BaseModel):
    action: str      # approve | reject | requeue
    note: str | None = None   # with requeue: what's on screen, for the AI review and the title


@app.post("/api/clips/{clip_id}/action")
def post_clip_action(clip_id: int, body: ClipAction, request: Request):
    try:
        return dashboard.clip_action(svc(request)["db"], clip_id, body.action, body.note)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


class ChannelIn(BaseModel):
    url: str
    record_enabled: bool = True                 # record + clip locally (the reason to add a streamer)
    permission_confirmed: bool = False
    notes: str | None = None


class ChannelPatch(BaseModel):
    enabled: bool | None = None
    record_enabled: bool | None = None          # record + clip locally
    vod_enabled: bool | None = None             # also clip this channel's past streams (VOD mode)
    permission_confirmed: bool | None = None    # creator agreed — required to PUBLISH (Phase 9)
    chat_delay_seconds: float | None = None     # used when chat_delay_auto is off or not yet measured
    chat_delay_auto: bool | None = None         # derive delay from the stream's measured latency
    layout: str | None = None                   # auto (default: split if a facecam is detected) | blur | split
    facecam_rect: str | None = None             # manual "x,y,w,h" fractions; overrides detection
    notes: str | None = None


@app.get("/api/channels")
def get_channels(request: Request):
    return ch.list_channels(svc(request)["db"])


@app.post("/api/channels", status_code=201)
def post_channel(body: ChannelIn, request: Request):
    s = svc(request)
    try:
        return ch.add_channel(s["db"], s["api"], s["web"], body.url, body.permission_confirmed, body.notes,
                              body.record_enabled)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except ch.ChannelExists as e:
        raise HTTPException(409, str(e))
    except QuotaExceeded as e:
        raise HTTPException(429, str(e))
    except YouTubeApiError as e:
        raise HTTPException(502, str(e))


@app.patch("/api/channels/{channel_id}")
def patch_channel(channel_id: str, body: ChannelPatch, request: Request):
    try:
        row = ch.update_channel(svc(request)["db"], channel_id, **body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not row:
        raise HTTPException(404, "channel not found")
    return row


@app.delete("/api/channels/{channel_id}", status_code=204)
def delete_channel(channel_id: str, request: Request):
    s = svc(request)
    try:
        ended = ch.remove_channel(s["db"], channel_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    for session in ended:            # stop recording/chat now; footage is wiped after the usual grace period
        s["recorders"].on_session_ended(session)
        s["chat"].on_session_ended(session)


@app.get("/api/sessions")
def get_sessions(request: Request, status: str | None = None, limit: int = 50):
    db = svc(request)["db"]
    if status:
        rows = db.query("SELECT * FROM live_sessions WHERE status = ? ORDER BY detected_at DESC LIMIT ?",
                        (status, limit))
    else:
        rows = db.query("SELECT * FROM live_sessions ORDER BY detected_at DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


@app.get("/api/recordings")
def get_recordings(request: Request, limit: int = 50):
    s = svc(request)
    rows = s["db"].query(
        """SELECT r.*, l.video_id, l.title,
                  (SELECT COUNT(*) FROM segments g WHERE g.recording_id = r.id) AS segments
             FROM recordings r JOIN live_sessions l ON l.id = r.session_id
            ORDER BY r.id DESC LIMIT ?""", (limit,))
    return {"active": [r.session["video_id"] for r in s["recorders"].recorders.values() if r.is_alive()],
            "recent": [dict(r) for r in rows]}


@app.get("/api/storage")
def get_storage(request: Request):
    s = svc(request)
    summary = storage_summary(s["db"], s["settings"].recordings_dir)
    return {**summary,
            "recordings_gb": round(summary["recordings_bytes"] / 1024**3, 2),
            "disk_free_gb": round(disk_free_gb(s["settings"].recordings_dir), 1),
            "min_free_gb": s["settings"].min_free_gb,
            "buffer_minutes": s["settings"].buffer_minutes}


@app.get("/api/chat")
def get_chat_status(request: Request):
    s = svc(request)
    runs = s["db"].query(
        """SELECT r.*, l.video_id FROM chat_runs r JOIN live_sessions l ON l.id = r.session_id
            ORDER BY r.id DESC LIMIT 20""")
    return {"active": s["chat"].active(), "recent_runs": [dict(r) for r in runs]}


@app.get("/api/sessions/{video_id}/chat")
def get_session_chat(video_id: str, request: Request, minutes: int = 30):
    s = svc(request)
    row = s["db"].one("SELECT id FROM live_sessions WHERE video_id = ?", (video_id,))
    if not row:
        raise HTTPException(404, "session not found")
    width = s["settings"].chat_bucket_seconds
    return {"bucket_seconds": width, "buckets": s["chat_store"].buckets(row["id"], limit=minutes * 60 // width)}


@app.get("/api/spikes")
def get_spikes(request: Request, video_id: str | None = None, limit: int = 50):
    db = svc(request)["db"]
    sql = """SELECT k.*, l.video_id, l.title FROM spikes k JOIN live_sessions l ON l.id = k.session_id"""
    params: tuple = ()
    if video_id:
        sql += " WHERE l.video_id = ?"
        params = (video_id,)
    rows = db.query(sql + " ORDER BY k.event_utc DESC LIMIT ?", (*params, limit))
    return [dict(r) for r in rows]


@app.post("/api/sessions/{video_id}/redetect")
def redetect(video_id: str, request: Request, minutes: float = 240):
    """Re-run spike detection over the last `minutes` of a session's chat (e.g. after tuning thresholds)."""
    s = svc(request)
    row = s["db"].one("SELECT id, status FROM live_sessions WHERE video_id = ?", (video_id,))
    if not row:
        raise HTTPException(404, "session not found")
    s["chat_store"].aggregate(row["id"], lookback_seconds=int(minutes * 60))
    touched = s["spikes"].run(row["id"], final=row["status"] == "ended", lookback_minutes=minutes)
    return {"spikes_written": touched}


@app.get("/api/clips")
def get_clips(request: Request, status: str | None = None, limit: int = 50):
    db = svc(request)["db"]
    sql = """SELECT k.*, l.video_id, l.title, l.channel_id, l.actual_start_at, c.title channel_title, c.handle,
                    c.permission_confirmed, e.llm_result, q.status publish_status, q.remote_url
               FROM clips k JOIN live_sessions l ON l.id = k.session_id
               LEFT JOIN channels c ON c.channel_id = l.channel_id
               LEFT JOIN evaluations e ON e.clip_id = k.id
               LEFT JOIN publish_queue q ON q.clip_id = k.id AND q.platform = 'youtube'"""
    params: tuple = ()
    if status:
        sql += " WHERE k.status = ?"
        params = (status,)
    return [dict(r) for r in db.query(sql + " ORDER BY k.id DESC LIMIT ?", (*params, limit))]


@app.get("/clips/{clip_id}/{kind}.mp4")
def clip_video(clip_id: int, kind: str, request: Request):
    if kind not in ("vertical", "final", "source"):
        raise HTTPException(404, "unknown rendition")
    row = svc(request)["db"].one(f"SELECT {kind}_path p FROM clips WHERE id = ?", (clip_id,))
    if not row or not row["p"] or not Path(row["p"]).exists():
        raise HTTPException(404, "clip not rendered" if kind != "source" else "no footage for this clip")
    if kind == "source":   # raw footage preview for clips that aren't rendered (yet)
        return FileResponse(playable_source(Path(row["p"]), svc(request)["settings"].ffmpeg_bin), media_type="video/mp4")
    return FileResponse(row["p"], media_type="video/mp4")


def playable_source(source: Path, ffmpeg_bin: str) -> Path:
    """Past-stream footage is already MP4. Live footage is MPEG-TS, which browsers can't play: copy it into an MP4
    once (no re-encoding, about a second). make_zero keeps file time 0 = the source's first frame, which is what
    the dashboard's #t= offset (window_start_pts - source_start_pts) assumes."""
    if source.suffix.lower() == ".mp4":
        return source
    preview = source.with_name("preview.mp4")
    if not preview.exists():
        tmp = source.with_name("preview.tmp.mp4")
        subprocess.run([ffmpeg_bin, "-v", "error", "-y", "-i", str(source), "-c", "copy", "-avoid_negative_ts",
                        "make_zero", "-movflags", "+faststart", str(tmp)], check=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        tmp.replace(preview)
    return preview


@app.post("/api/clips/{clip_id}/rerender")
def rerender_clip(clip_id: int, request: Request):
    """Rebuild final.mp4 with the current layout/caption settings (no re-transcription)."""
    s = svc(request)
    try:
        path = s["caption_worker"].rerender(clip_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    row = s["db"].one("SELECT layout FROM clips WHERE id = ?", (clip_id,))
    return {"final_path": str(path), "layout": row["layout"]}


@app.get("/api/clips/{clip_id}/evaluation")
def clip_evaluation(clip_id: int, request: Request):
    row = svc(request)["db"].one("SELECT * FROM evaluations WHERE clip_id = ?", (clip_id,))
    if not row:
        raise HTTPException(404, "not evaluated yet")
    d = dict(row)
    for k in ("signals", "llm_result", "reasons"):
        d[k] = json.loads(d[k]) if d[k] else None
    return d


@app.get("/api/clips/{clip_id}/transcript")
def clip_transcript(clip_id: int, request: Request):
    row = svc(request)["db"].one("SELECT * FROM transcripts WHERE clip_id = ?", (clip_id,))
    if not row:
        raise HTTPException(404, "no transcript")
    return {**dict(row), "words": json.loads(row["words"])}


@app.get("/api/publish")
def publish_status(request: Request, limit: int = 50):
    s = svc(request)
    q = s["publish_queue"]
    enqueue_approved(s["db"])
    rows = s["db"].query("""SELECT q.*, x.score, x.final_path, l.title stream_title FROM publish_queue q
                              JOIN clips x ON x.id = q.clip_id JOIN live_sessions l ON l.id = x.session_id
                             ORDER BY CASE q.status WHEN 'uploading' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                                      q.priority DESC, q.id DESC LIMIT ?""", (limit,))
    items = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
        items.append(d)
    return {"enabled": q.enabled, "privacy": q.privacy, "waiting_because": q.why_not_now(),
            "youtube_sign_in": sign_in_status(s["settings"].youtube_token_path),
            "published_today": q.published_today(datetime.now().astimezone()), "max_per_day": q.max_per_day,
            "blocked_no_permission": blocked_by_permission(s["db"]), "items": items}


@app.get("/api/publish/history")
def publish_history(request: Request, limit: int = 100):
    rows = svc(request)["db"].query(
        """SELECT a.*, q.clip_id, q.title, q.remote_url FROM publish_attempts a
             JOIN publish_queue q ON q.id = a.queue_id ORDER BY a.id DESC LIMIT ?""", (limit,))
    return [dict(r) for r in rows]


@app.post("/api/clips/{clip_id}/metadata/preview")
def preview_metadata(clip_id: int, request: Request):
    """Generate title/description/tags for any clip without queueing or uploading it."""
    s = svc(request)
    if not s["db"].one("SELECT 1 FROM clips WHERE id = ?", (clip_id,)):
        raise HTTPException(404, "clip not found")
    md = s["metadata_writer"].write(clip_context(s["db"], clip_id))
    return {"title": md.title, "description": md.description, "tags": md.tags, "source": md.source,
            "candidates": [{"title": t, "score": sc} for t, sc in md.candidates]}


class MetadataEdit(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None


@app.patch("/api/publish/{queue_id}")
def edit_queued(queue_id: int, body: MetadataEdit, request: Request):
    """Hand-edit a queued item's metadata before it goes out."""
    db = svc(request)["db"]
    row = db.one("SELECT status FROM publish_queue WHERE id = ?", (queue_id,))
    if not row:
        raise HTTPException(404, "not in queue")
    if row["status"] not in ("queued", "failed"):
        raise HTTPException(409, f"can't edit an item that is {row['status']}")
    fields = {k: (json.dumps(v) if k == "tags" else v) for k, v in body.model_dump().items() if v is not None}
    if fields:
        fields["metadata_source"] = "manual"
        cols = ", ".join(f"{k} = ?" for k in fields)
        db.execute(f"UPDATE publish_queue SET {cols} WHERE id = ?", (*fields.values(), queue_id))
    return dict(db.one("SELECT * FROM publish_queue WHERE id = ?", (queue_id,)))


@app.post("/api/publish/{queue_id}/cancel")
def cancel_queued(queue_id: int, request: Request):
    db = svc(request)["db"]
    n = db.execute("UPDATE publish_queue SET status = 'cancelled', updated_at = ? WHERE id = ? AND status IN "
                   "('queued', 'failed')", (datetime.now().astimezone().isoformat(timespec="seconds"), queue_id)).rowcount
    if not n:
        raise HTTPException(409, "only queued or failed items can be cancelled")
    return {"cancelled": queue_id}


class VodLink(BaseModel):
    url: str


class VodAction(BaseModel):
    action: str      # retry | skip


@app.post("/api/vods", status_code=201)
def post_vod(body: VodLink, request: Request):
    """Clip a past stream by its link (any age or title; the channel must be added)."""
    try:
        return svc(request)["vod_scanner"].add_link(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/vods/scan")
def scan_vods(request: Request):
    return {"new": len(svc(request)["vod_scanner"].scan())}


@app.post("/api/vods/{job_id}/action")
def post_vod_action(job_id: int, body: VodAction, request: Request):
    try:
        return dashboard.vod_action(svc(request)["db"], job_id, body.action)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/monitor/poll")
def poll_now(request: Request):
    return asdict(svc(request)["monitor"].poll())


@app.get("/api/status")
def status(request: Request):
    s = svc(request)
    db = s["db"]
    return {
        "api_key_configured": bool(s["api"].api_key),
        "quota_used_today": s["ledger"].used(),
        "quota_budget": s["ledger"].daily_budget,
        "channels": db.one("SELECT COUNT(*) n FROM channels")["n"],
        "live_now": db.one("SELECT COUNT(*) n FROM live_sessions WHERE status = 'live'")["n"],
        "recent_errors": [dict(r) for r in db.query("SELECT * FROM errors ORDER BY id DESC LIMIT 10")],
    }
