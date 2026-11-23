# YouTube upload setup (OAuth, one time)

Uploading Shorts needs your permission on **your** YouTube channel, which means OAuth: a sign-in in your browser, not just the API key. It's free. Allow about 10 minutes.

Use the **same Google Cloud project** as your API key (`gta-clipper`).

## 1. OAuth consent screen
1. Open https://console.cloud.google.com/auth/overview (if it's your first time, click **Get started**).
2. **App name:** `GTA Clipper` · **User support email:** your email → **Next**.
3. **Audience:** **External** → **Next**.
4. **Contact email:** your email → **Next** → agree → **Create**.
5. Go to **Audience** in the left menu → **Test users** → **+ Add users** → add the **exact Gmail address you'll pick in the sign-in window** (the account that owns the YouTube channel; for a Brand Account channel, your personal Gmail that manages it) → **Save**.
   > If you skip this or sign in with a different account, Google shows **"Error 403: access_denied … can only be accessed by developer-approved testers"**.

**Testing vs production.** Leave the app in **Testing**; that's enough. The trade-off: Google expires a Testing app's sign-in after **7 days**, so once a week you re-run step 4 (about 30 s). Queued uploads wait meanwhile and the dashboard/`/api/publish` tells you when sign-in is needed.
Publishing to **production** removes the weekly re-login, but Google asks for a home page, privacy policy and terms-of-service link. Those can be free pages (GitHub Pages / Google Sites); do it later if the weekly sign-in gets annoying.

## 2. Data access (scope)
1. Left menu **Data Access** → **Add or remove scopes**.
2. Tick **`.../auth/youtube.upload`** ("Manage your YouTube videos") **and `.../auth/youtube.readonly`** (lets the app confirm which channel it's connected to). If they aren't in the list, paste them into "Manually add scopes":
   ```
   https://www.googleapis.com/auth/youtube.upload
   https://www.googleapis.com/auth/youtube.readonly
   ```
3. **Update** → **Save**.

## 3. Create the Desktop client
1. Left menu **Clients** → **+ Create client**.
2. **Application type:** **Desktop app** · **Name:** `clipper-desktop` → **Create**.
3. Click **Download JSON** on the dialog (or the download icon in the client list).
4. Save it as **`data\client_secret.json`** inside the project folder (create `data` if it doesn't exist yet).

> Treat this file like a password. It's inside `data/`, which git ignores.

## 4. Sign in once
In a terminal:
```powershell
cd <project folder>
.\.venv\Scripts\python -m app.publish.auth
```
A browser opens:
1. Choose the account that owns the YouTube channel. **If you have several channels (brand accounts), pick the channel the Shorts should go to.**
2. "Google hasn't verified this app" → **Advanced** → **Go to GTA Clipper (unsafe)**. This is your own app.
3. Allow **Manage your YouTube videos** and **View your YouTube account** → you'll see "Signed in. You can close this tab".

The terminal prints the channel it connected to. The sign-in is saved to `data/youtube_token.json` and refreshes itself, for 7 days while the app is in Testing; then run this command again.

## 5. Check the channel can post Shorts
- Uploads over 15 minutes need a verified phone number. Ours are 40 s, so this doesn't apply.
- New channels can have lower daily upload limits. The app's daily cap (`PUBLISH_MAX_PER_DAY`, default 6) stays well under them.

## Settings (`.env`)
```
PUBLISH_ENABLED=true          # master switch for the upload scheduler
PUBLISH_PRIVACY=public        # private | unlisted | public (see "private lock" below)
PUBLISH_INTERVAL_MINUTES=30   # at least this long between uploads
PUBLISH_MAX_PER_DAY=6         # daily cap (Pacific-time day, like YouTube quota)
PUBLISH_SLOTS=12:00,14:30,17:00,19:00,21:00,23:00   # posting slots (empty = post as soon as ready)
PUBLISH_SLOTS_TIMEZONE=America/New_York             # the audience's clock
```
How clips are picked for each slot, the breaking lane and expiry: [architecture.md §7](architecture.md#7-publishing-apppublish).

## The "private lock" on unaudited projects
YouTube's policy is that videos uploaded through the API by a Google Cloud project that hasn't passed the
*YouTube API Services audit* can be locked to **private**. In practice it varies: in November 2026 this project
(new, unaudited) had its uploads come out **public**. So the app doesn't assume either way: it asks for
`PUBLISH_PRIVACY`, then checks what YouTube actually applied about 10 minutes after each upload. A locked upload
shows up under **Recent problems** on the dashboard, and the phone alert carries an **Open in Studio** button.

If your uploads are locked, open each Short in YouTube Studio and set it to **Public** by hand, or apply for the audit.

To lift the lock (free, reviewed by Google, usually takes days to weeks):
1. Fill in the YouTube API Services audit and quota extension form: https://support.google.com/youtube/contact/yt_api_form
2. Describe the app honestly: a personal tool that uploads clips of streamers who gave permission, with credit and a link to the original.
3. Expect to need a privacy policy and terms page. The same pages also move the OAuth app out of Testing
   mode, which ends the weekly sign-in (see `docs/future-ideas.md`).

## What gets published
Only clips that are **approved** by the AI review (or by you) **and** come from a channel with **Upload allowed**
switched on (dashboard → Channels; `permission_confirmed` in the database). Switch it on only once the creator has agreed.
