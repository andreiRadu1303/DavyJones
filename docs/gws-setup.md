# Connecting Google Workspace to your DavyJones agent

DavyJones uses **Bring-Your-Own OAuth Client** (BYOC) to give your cloud agent
access to your Google account. You create a small OAuth app in your own Google
Cloud project and the agent uses your credentials to read/write your Gmail,
Drive, and Calendar.

This means:
- DavyJones never sees your Google data — your tokens go straight from Google
  to your isolated cloud agent.
- No Google verification process. Your app stays in **Testing** with you as the
  only test user.
- You pick which scopes the agent gets.

Setup takes about 10 minutes the first time.

## 1. Create a Google Cloud project

1. Open [console.cloud.google.com](https://console.cloud.google.com/) and sign
   in with the Google account whose Gmail/Drive/Calendar you want the agent to
   access.
2. Click the project picker in the top bar → **New Project**. Name it whatever
   you like ("DavyJones Personal", say). Click **Create**.

## 2. Enable the APIs you want

For each API the agent should be able to use, click the link below and hit
**Enable**. (You only need to enable the ones you care about.)

- [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com)
- [Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)
- [Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)
- [Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com)
- [Docs API](https://console.cloud.google.com/apis/library/docs.googleapis.com)

Make sure the project picker at the top is showing the project you just
created before clicking Enable.

## 3. Configure the OAuth consent screen

Go to **APIs & Services → OAuth consent screen**
([direct link](https://console.cloud.google.com/apis/credentials/consent)).

1. Pick **External** and click Create.
2. Fill in app name (e.g. "My DavyJones agent"), user support email = yours,
   developer contact email = yours. Skip the rest.
3. **Scopes** screen: click *Add or remove scopes*, then check the scopes you
   want the agent to use. A reasonable starting set:
   - `.../auth/gmail.readonly`
   - `.../auth/gmail.send`
   - `.../auth/drive.file`
   - `.../auth/calendar`
   - `.../auth/spreadsheets`
   - `.../auth/documents`
4. **Test users** screen: add your own email. You're the only test user.
5. Save.

The app stays in **Testing** mode — that's correct. Test mode allows up to
100 users without verification; you only ever add yourself.

## 4. Create the OAuth client

Go to **APIs & Services → Credentials**
([direct link](https://console.cloud.google.com/apis/credentials)).

1. Click **Create Credentials → OAuth client ID**.
2. Application type: **Web application**.
3. Name: anything, e.g. "DavyJones cloud agent".
4. Under **Authorized redirect URIs**, click *Add URI* and paste exactly:

   ```
   https://34-77-180-124.nip.io/api/v1/auth/gws/callback
   ```

   (The plugin shows you this URL with a Copy button — use that to avoid typos.)
5. Click **Create**.
6. Google shows you a **Client ID** and **Client secret**. Copy both.

## 5. Connect from the plugin

1. In Obsidian → DavyJones Control Panel → Google Workspace → click
   **Authenticate**.
2. Paste the Client ID and Client Secret. Click **Connect**.
3. A browser tab opens to Google's sign-in. Sign in with the same email
   you added as a test user.
4. You'll see a warning that **"Google hasn't verified this app"** — that's
   expected because your app is in test mode. Click **Continue → Continue**.
5. Approve the requested scopes.
6. The browser shows "DavyJones · Google Workspace connected" — you can close
   the tab. The plugin auto-detects success within a few seconds.

You're done. The agent in your vault now has refresh-token-based access to
the scopes you granted.

## Re-authenticating

Test-mode refresh tokens **expire after 7 days**. When that happens, the
agent will start failing Google API calls. Just go back to the Authenticate
button and run through the flow again — same Client ID/Secret, same flow,
new refresh token.

If you outgrow this and want long-lived tokens, take your OAuth app through
Google's verification (paperwork-only since these are non-restricted scopes).
That's a separate process documented at
[support.google.com/cloud/answer/9110914](https://support.google.com/cloud/answer/9110914).

## Troubleshooting

**"Google didn't return a refresh token"** — happens if you've authorized
this OAuth client before and Google decides the existing grant is enough.
Visit [myaccount.google.com/permissions](https://myaccount.google.com/permissions),
remove your test app, and try again.

**"Invalid or expired state token"** — the OAuth state token expires after
10 minutes. Restart the connect flow.

**"Connection failed: invalid_client"** — the Client ID or Client Secret you
pasted doesn't match what's in Google Cloud, or the redirect URI on your
OAuth client doesn't exactly match the one in the plugin.

**"Access blocked: this app's request is invalid"** — the OAuth consent
screen wasn't fully configured (probably missing scopes). Go back to step 3
and finish it.
