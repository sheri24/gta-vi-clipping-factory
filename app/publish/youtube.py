"""YouTube Data API uploads (videos.insert) with the user's OAuth token. Free; 1 quota unit per upload, from its own
allowance of 100 uploads a day per Google project (YouTube quota table, checked 2026-11-23)."""

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.net import is_network_error
from app.private_files import write_private

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",     # upload videos
    "https://www.googleapis.com/auth/youtube.readonly",   # confirm which channel we're connected to
]


TESTING_TOKEN_DAYS = 7   # Google expires refresh tokens of "Testing" OAuth apps after 7 days


def sign_in_info_path(token_path: Path) -> Path:
    return token_path.with_name("youtube_sign_in.json")


def record_sign_in(token_path: Path, channel_title: str, channel_id: str) -> None:
    sign_in_info_path(token_path).write_text(json.dumps({
        "signed_in_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channel_title": channel_title, "channel_id": channel_id}), encoding="utf-8")


def sign_in_status(token_path: Path) -> dict:
    """Who we're connected to and when a Testing-mode sign-in runs out (for the dashboard)."""
    info_path = sign_in_info_path(token_path)
    if not token_path.exists() or not info_path.exists():
        return {"signed_in": token_path.exists(), "channel": None, "expires_around": None, "renew_soon": False}
    info = json.loads(info_path.read_text(encoding="utf-8"))
    expires = datetime.fromisoformat(info["signed_in_at"]) + timedelta(days=TESTING_TOKEN_DAYS)
    return {"signed_in": True, "channel": info["channel_title"], "channel_id": info["channel_id"],
            "expires_around": expires.isoformat(timespec="minutes"),
            "renew_soon": expires - datetime.now(timezone.utc) < timedelta(days=1)}


class PublishError(Exception):
    def __init__(self, message: str, kind: str = "retry"):
        super().__init__(message)
        self.kind = kind     # retry | network (internet down) | quota (after the Pacific reset) | auth | fatal


@dataclass
class UploadResult:
    remote_id: str
    url: str
    privacy: str | None = None     # what YouTube says it applied (may differ from what we asked)


LOCKED_PRIVATE_HELP = ("YouTube set this upload to private although public was requested. Uploads through the API "
                       "from an unaudited Google project are locked to private; request the free YouTube API Services "
                       "audit (see docs/setup-youtube-upload.md). Until then, make each Short public in YouTube Studio.")


def load_credentials(token_path: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not token_path.exists():
        raise PublishError("not signed in to YouTube: run `python -m app.publish.auth`", "auth")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                if is_network_error(e) or type(e).__name__ == "TransportError":   # offline, not signed out
                    raise PublishError(f"no internet: couldn't reach Google to refresh the sign-in ({e})",
                                       "network") from e
                raise PublishError(f"YouTube sign-in expired or revoked ({e}); run `python -m app.publish.auth`",
                                   "auth") from e
            write_private(token_path, creds.to_json())
        else:
            raise PublishError("YouTube token invalid: run `python -m app.publish.auth`", "auth")
    return creds


def classify_http_error(e) -> PublishError:
    text = str(e)
    status = getattr(getattr(e, "resp", None), "status", None)
    if "quotaExceeded" in text or "uploadLimitExceeded" in text or "rateLimitExceeded" in text:
        return PublishError(f"YouTube upload limit reached: {text[:200]}", "quota")
    if status in (401, 403) and ("forbidden" in text.lower() or "unauthorized" in text.lower()
                                 or "insufficientPermissions" in text):
        return PublishError(f"YouTube refused the upload (permissions): {text[:200]}", "auth")
    if status in (400,) and ("invalidTitle" in text or "invalidDescription" in text or "invalidTags" in text):
        return PublishError(f"YouTube rejected the metadata: {text[:200]}", "fatal")
    return PublishError(f"upload failed: {text[:300]}", "retry")


class YouTubePublisher:
    platform = "youtube"

    def __init__(self, token_path: Path, category_id: str = "20"):
        self.token_path = token_path
        self.category_id = category_id
        self._ready_cache: tuple[float, tuple[bool, str]] | None = None

    def ready(self) -> tuple[bool, str]:
        """Signed in AND the sign-in still refreshes (a Testing-mode sign-in dies after 7 days).
        Checked at most every 10 min so the scheduler doesn't hammer Google."""
        if not self.token_path.exists():
            return False, "not signed in to YouTube (run `python -m app.publish.auth`)"
        if self._ready_cache and time.monotonic() - self._ready_cache[0] < 600:
            return self._ready_cache[1]
        try:
            load_credentials(self.token_path)
            result = (True, "ok")
        except PublishError as e:
            result = (False, str(e))
        self._ready_cache = (time.monotonic(), result)
        return result

    def upload(self, video: Path, title: str, description: str, tags: list[str], privacy: str) -> UploadResult:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        creds = load_credentials(self.token_path)
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        body = {
            "snippet": {"title": title, "description": description, "tags": tags, "categoryId": self.category_id},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False},
        }
        media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        try:
            response = None
            while response is None:            # resumable: survives brief network hiccups chunk by chunk
                _, response = request.next_chunk(num_retries=3)
        except HttpError as e:
            raise classify_http_error(e) from e
        except Exception as e:                 # network errors etc.
            raise PublishError(f"upload interrupted: {e}", "network" if is_network_error(e) else "retry") from e
        vid = response["id"]
        return UploadResult(vid, f"https://youtube.com/shorts/{vid}", (response.get("status") or {}).get("privacyStatus"))

    def privacy_of(self, remote_id: str) -> str | None:
        """Current privacy of an uploaded video (1 quota unit). None if YouTube no longer has it."""
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError

        yt = build("youtube", "v3", credentials=load_credentials(self.token_path), cache_discovery=False)
        try:
            items = yt.videos().list(part="status", id=remote_id).execute().get("items") or []
        except HttpError as e:
            raise classify_http_error(e) from e
        return items[0]["status"].get("privacyStatus") if items else None
