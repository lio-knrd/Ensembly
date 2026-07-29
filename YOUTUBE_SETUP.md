# YouTube publishing setup

Everything needed to turn `Publish to YouTube` from "not configured" into a
working upload. Budget ~15 minutes; only steps 1-5 are required before the first
upload.

The app needs exactly two values in `.env`:

```
YOUTUBE_CLIENT_ID=
YOUTUBE_CLIENT_SECRET=
```

Both come from **one** OAuth client in a Google Cloud project. That single client
authorizes any number of channels — each channel is linked once in the app, and
the per-channel tokens live in the database, never in `.env`.

---

## 1. Create a Google Cloud project

1. Open <https://console.cloud.google.com/projectcreate> (any Google account).
2. Name it (e.g. `ensembly-publishing`) and create it.
3. Make sure it is the selected project in the top bar for every step below.

No billing account is required — the YouTube Data API is free within its quota.

## 2. Enable the YouTube Data API v3

1. Go to <https://console.cloud.google.com/apis/library/youtube.googleapis.com>.
2. Click **Enable**.

Without this, every call fails with `accessNotConfigured`.

## 3. Configure the consent screen

This lives under **Google Auth platform** (the console's newer name for what the
older docs call "APIs & Services > OAuth consent screen"), split across three
pages. Scopes are **not** part of creating the OAuth client in step 4 — they are
their own page here.

1. **Branding** — app name, user support email, developer contact email.
2. **Audience** — **User type: External.** (Internal only exists for Workspace
   organizations; it is greyed out for a personal Google account.)
3. **Data Access > Add or remove scopes** — add these two:
   - `https://www.googleapis.com/auth/youtube.upload` (required, this is what uploads)
   - `https://www.googleapis.com/auth/youtube.readonly` (only so the app can show
     the channel's name and avatar on the account card)

   **The list only offers scopes of APIs that are already enabled.** If neither
   shows up, step 2 was skipped — enable the YouTube Data API v3 first, then
   reload this page.
4. **Test users** (on the Audience page) — only relevant while the publishing
   status is *Testing*: just the Google accounts allowed to authorize the app.
   Enter the personal Google account that clicks "Allow" when linking — the one
   that owns or manages the channel. A Brand Account is never entered here; it
   has no login of its own. Pressing **Publish app** makes this list irrelevant.

**Publishing status matters.** Leave it in *Testing* and Google issues refresh
tokens that **expire after 7 days**, so the channel needs re-linking every week.
Press **Publish app** to stop that. With sensitive scopes an unverified published
app shows an "unverified app" warning during login (click *Advanced > Go to ...*)
and is capped at 100 users — fine for a personal tool. Full verification is only
worth it if other people will link their channels.

## 4. Create the OAuth client

Under **Google Auth platform > Clients > Create client** (older console:
*APIs & Services > Credentials > Create credentials > OAuth client ID*). There is
deliberately no scope picker on this form — scopes belong to step 3.

1. **Application type: Web application.**
2. Under **Authorized redirect URIs**, add exactly:

   ```
   http://localhost:8420/api/youtube/link/callback
   ```

   Google exempts localhost from its https rule, so no tunnel is needed — unlike
   TikTok. If `APP_PORT` is not 8420, change the port here and set
   `YOUTUBE_REDIRECT_URI` in `.env` to the same string. It must match verbatim,
   including the scheme and trailing path.
3. Create it and copy the **Client ID** and **Client secret**.

## 5. Put the credentials in `.env` and link the channel

```
YOUTUBE_CLIENT_ID=1234567890-abcdef.apps.googleusercontent.com
YOUTUBE_CLIENT_SECRET=GOCSPX-...
```

Restart the app, then:

1. **Settings > YouTube publishing > Link channel.** A Google tab opens; approve
   the two scopes. The tab redirects back to the app and the channel appears in
   the list on its own.
2. **Settings > Content presets > (your group) > YouTube channel** — pick the
   channel this group publishes to. It saves immediately.
3. Open a finished project. **Final video > Publish to YouTube.**

The title, description, and tags are pre-filled from the project's
`final/metadata.json` and stay editable before uploading.

---

## The three limits worth knowing before you rely on this

**1. Uploads are locked to private until Google audits the API project.**
Videos inserted through the API by an unaudited project created after 28 July
2020 are forced to `private`, whatever privacy level was requested. The app
reports the downgrade after an upload rather than pretending it worked. Two ways
to live with it:

- Upload as private and flip it to public in YouTube Studio (this is the normal
  path, and the counterpart of the TikTok draft flow).
- Or set a `publishAt` schedule, which also requires private.

To lift it, submit the **YouTube API Services Compliance Audit** form linked from
the API's Quotas page. It is the same submission used to request more quota.

**2. 100 uploads per day.** `videos.insert` has its own quota bucket — 100 calls
per day per project — separate from the 10,000-unit daily pool the read
endpoints share. The counter resets at midnight Pacific time. Reading statuses
and setting thumbnails come out of the 10,000-unit pool (1 and 50 units), so
they are effectively free at this scale.

**3. Refresh tokens die after 7 days while the consent screen is in Testing.**
See step 3. When it happens, the account card shows *re-link needed* and linking
again fixes it.

## What happens to a video, and what makes it a Short

There is no Shorts endpoint. YouTube decides from the file: a **vertical (9:16)
or square** video **up to 3 minutes** is served as a Short. This pipeline renders
9:16 at 60-90 seconds by default, so its output qualifies as-is. Nothing needs
`#Shorts` in the title.

Every upload declares `containsSyntheticMedia: true` — YouTube's disclosure for
AI-generated or altered content — because everything this app renders is
AI-generated. That is set in code, not left to the operator, exactly like the
`is_aigc` flag on the TikTok path. `selfDeclaredMadeForKids` defaults to false
and is sent on every upload because YouTube requires the declaration.

The optional **"Use the title card as the thumbnail"** checkbox calls
`thumbnails.set`, which needs a phone-verified channel
(<https://youtube.com/verify>). If it fails the video still uploads; the error is
reported separately.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `redirect_uri_mismatch` | The URI in step 4 does not match `Settings > YouTube publishing > Redirect URI` character for character. |
| `access_denied` right after consent | The Google account is not in **Test users** and the app is still in Testing. |
| The youtube scopes are missing from the scope picker | The YouTube Data API v3 is not enabled yet (step 2). The picker only lists scopes of enabled APIs. |
| `accessNotConfigured` / "has not been used in project N before or it is disabled" | Same cause, seen from the other side: the scopes were added by hand but step 2 was skipped. Open the link in the error message (it preselects the right project), press **Enable**, wait a minute, link again. |
| "Google did not return a refresh token" | A prior grant is still active. Remove the app at <https://myaccount.google.com/permissions> and link again. |
| `invalid_grant` on publish | Refresh token expired (7-day Testing limit) or was revoked. Link the channel again. |
| "This Google account has no YouTube channel" | The account never created a channel. Create one at youtube.com, then link again. |
| Video is private although public was chosen | Expected until the compliance audit. See limit 1. |
| `uploadLimitExceeded` | 100 uploads used today; resets midnight Pacific. |
