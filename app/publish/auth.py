"""One-time YouTube sign-in:  .venv\\Scripts\\python -m app.publish.auth

Opens your browser, asks Google for permission to upload, saves data/youtube_token.json,
then prints which channel it connected to. See docs/setup-youtube-upload.md.
"""

import sys

from app.private_files import write_private
from app.config import get_settings
from app.publish.youtube import SCOPES, record_sign_in


def main() -> int:
    s = get_settings()
    if not s.client_secret_path.exists():
        print(f"Missing {s.client_secret_path}. Follow docs/setup-youtube-upload.md step 3 first.")
        return 1
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    flow = InstalledAppFlow.from_client_secrets_file(str(s.client_secret_path), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline",
                                  authorization_prompt_message="Opening your browser to sign in to YouTube...",
                                  success_message="Signed in. You can close this tab and go back to the terminal.")
    write_private(s.youtube_token_path, creds.to_json())        # only this Windows account can read it
    me = build("youtube", "v3", credentials=creds, cache_discovery=False).channels().list(
        part="snippet", mine=True).execute()
    items = me.get("items", [])
    if not items:
        print("Signed in, but this Google account has no YouTube channel. Create one, then run this again.")
        return 1
    ch = items[0]
    record_sign_in(s.youtube_token_path, ch["snippet"]["title"], ch["id"])
    print(f"Connected to YouTube channel: {ch['snippet']['title']} ({ch['id']})")
    print(f"Token saved to {s.youtube_token_path}. Uploads go to this channel.")
    print("While the Google app is in Testing mode this sign-in lasts 7 days; run this command again then.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
