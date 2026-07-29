# TikTok publishing setup

TikTok will not accept `localhost` as a redirect URI, and for apps created after
9 September 2024 it also requires **proof that you own the redirect URL**. So a
local install needs one public https hostname. This walks through the cheapest
route: a free ngrok dev domain.

The good news is the tunnel is only needed while *linking an account*. Once a
creator is linked, the refresh token lasts about a year and publishing runs
entirely from your machine — no tunnel, no public exposure.

The app needs these in `.env`:

```
TIKTOK_CLIENT_KEY=
TIKTOK_CLIENT_SECRET=
TIKTOK_REDIRECT_URI=
```

---

## 1. Get a stable public hostname (ngrok)

1. Sign up at <https://ngrok.com> and install the agent (Windows:
   `winget install ngrok.ngrok`).
2. Authenticate once with the token from the ngrok dashboard:

   ```bash
   ngrok config add-authtoken <YOUR_TOKEN>
   ```
3. The dashboard (**Domains**) shows a **dev domain** assigned to your account,
   of the form `some-random-words.ngrok-free.dev` (older accounts may have
   `.ngrok-free.app`). On the free plan it is not customizable, but it **stays
   the same across restarts** — which is what matters, because it gets
   registered with TikTok.
4. Start the tunnel against the app's port:

   ```bash
   ngrok http --domain=some-random-words.ngrok-free.dev 8420
   ```

   Newer agents renamed that flag; if `--domain` warns about deprecation, use
   `--url=https://some-random-words.ngrok-free.dev` instead. If `--url` errors
   with `unknown flag`, the agent is older and `--domain` is the one to use.

   Leave this running for the whole setup.

**Free-plan caveat:** ngrok shows an interstitial warning page in front of
browser traffic. It costs one extra click ("Visit Site") during login and does
not affect programmatic requests, so it should not interfere with TikTok
fetching the verification file. If TikTok's check fails anyway, that interstitial
is the first suspect — a paid ngrok plan or your own domain removes it.

## 2. Create the TikTok app

At <https://developers.tiktok.com>: create an app, and add the products
**Login Kit** and **Content Posting API**.

Under the app's settings, set the redirect URI to your tunnel plus the app's
callback path:

```
https://some-random-words.ngrok-free.dev/api/tiktok/link/callback
```

## 3. Verify the URL property

TikTok requires ownership proof for that URL before the app leaves sandbox.

1. On the app page, open **URL properties** and choose verification by
   **URL prefix**, entering `https://some-random-words.ngrok-free.dev/`.
2. Download the signature file TikTok generates.
3. Drop that file, unchanged, into:

   ```
   data/verification/
   ```

   Ensembly serves everything in that folder straight from the site root, so the
   file answers at `https://some-random-words.ngrok-free.dev/<filename>`. The
   folder sits outside `frontend/dist` on purpose — `npm run build` wipes that
   directory, and the proof has to survive rebuilds.
4. Confirm it is reachable (with the tunnel running):

   ```bash
   curl https://some-random-words.ngrok-free.dev/<filename>
   ```
5. Back in the TikTok portal, press **Verify**.

## 4. Fill in `.env` and link

```
TIKTOK_CLIENT_KEY=<from the app's "Credentials" section>
TIKTOK_CLIENT_SECRET=<same place>
TIKTOK_REDIRECT_URI=https://some-random-words.ngrok-free.dev/api/tiktok/link/callback
```

`TIKTOK_USE_PKCE` stays `false` for a web-type app; set it to `true` only if the
app is registered as a desktop client.

Restart the app, then **Settings > TikTok publishing > Link account**. Approve in
the tab that opens (clicking through ngrok's interstitial if it appears). The
callback lands back in the app and the account shows up.

If the browser cannot reach the tunnel for some reason, the panel also accepts
the redirected URL pasted by hand — copy the whole address bar contents from the
tab TikTok landed on and paste it into the field.

Finally, pick the account on a content preset (**Settings > Content presets >
your group > TikTok account**). Then any finished project in that group can be
published from its **Final video** panel.

Once an account is linked you can stop the tunnel. It is only needed again for
linking another account, or if TikTok re-checks the URL property.

---

## Limits worth knowing

- **Direct posts are forced private until TikTok audits the app.** The
  alternative in the publish panel — sending the video to the creator's TikTok
  **drafts** — needs no audit: the creator opens the inbox notification and posts
  it themselves at whatever audience they like. Max 5 pending drafts per creator
  per 24 hours.
- **The caption travels with the creator on the draft path.** TikTok's draft flow
  asks for the caption in the app, so the suggested caption in Ensembly is there
  to copy over.
- Everything uploaded declares `is_aigc` (AI-generated content), set in code
  because everything this pipeline renders is AI-generated.

## Troubleshooting

| Symptom | Cause |
|---|---|
| "Set TIKTOK_REDIRECT_URI in .env" | The variable is empty. TikTok, unlike Google, has no usable default — it must be the registered https URL. |
| TikTok rejects the redirect URI when saving the app | It is http, contains `localhost`, or is not absolute. |
| URL property verification fails | The tunnel is not running, the file is not in `data/verification/`, or ngrok's interstitial is answering instead of the file. Check with `curl` first. |
| "That login link expired. Start the link again." | The backend restarted between starting the link and the callback; the pending login lives in memory. Just link again. |
| Login works but nothing appears in the app | Check the server console for `[tiktok] link failed: ...` — the reason is also shown as a banner on the Settings page. |
