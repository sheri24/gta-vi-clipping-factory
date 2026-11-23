# Google Cloud setup: YouTube Data API key

Takes about 5 minutes. **No billing account or credit card is needed**: the YouTube Data API is free within its daily quota.

## 1. Create a project
1. Open https://console.cloud.google.com/ and sign in with the Google account you'll use for this project. Accept the terms if asked.
2. Click the **project picker** in the top bar (next to "Google Cloud") and choose **New project**.
3. Name it e.g. `gta-clipper`. Leave the organisation as "No organisation", then click **Create**.
4. Make sure the new project is selected in the project picker.

## 2. Enable the YouTube Data API v3
1. Open https://console.cloud.google.com/apis/library/youtube.googleapis.com
2. Click **Enable**.

## 3. Create an API key
1. Open https://console.cloud.google.com/apis/credentials
2. Click **+ Create credentials → API key**. Copy the key it shows.
3. Click **Edit API key** (or open the key from the list) and set:
   - **API restrictions → Restrict key →** tick only **YouTube Data API v3**.
   - Application restrictions: leave as **None**. The app runs on your laptop, and IP restriction breaks if your home IP changes.
4. Click **Save**. It can take a few minutes to take effect.

> Treat the key like a password. Anyone with it can spend your quota. It lives only in `.env`, which git ignores.

## 4. Put the key in the app
In the project folder, copy `.env.example` to `.env` and set:
```
YOUTUBE_API_KEY=AIza...your key...
```

## 5. (Optional) Check your quota buckets
https://console.cloud.google.com/apis/api/youtube.googleapis.com/quotas lists:
- the shared default bucket (normally **10,000 units/day**), which the monitor uses at roughly 1 unit/minute;
- the separate `videos.insert` and `search.list` allowances (100 calls/day each, 1 unit per call as of November 2026).
  Uploads use the `videos.insert` one; the app doesn't use `search.list`.

Quota resets at **midnight Pacific Time**. The app tracks its own spend per Pacific-time day and stops at `QUOTA_DAILY_BUDGET` (default 9,000) to keep a safety margin.

## Uploading
Uploading Shorts needs OAuth, not just a key: see [setup-youtube-upload.md](setup-youtube-upload.md). The API key alone
is enough to watch channels, record and make clips.
