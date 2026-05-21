[README.md](https://github.com/user-attachments/files/28110254/README.md)
# Resume bot

GitHub-hosted agent that watches the `jobs@r1concepts.com` inbox, scores each incoming resume against HR-managed filters using Claude, files the resumes into the right Google Drive folder, and logs every decision to a shared dashboard Sheet. A static GitHub Pages web app lets HR edit filters without touching code.

## How it works

```
Gmail (jobs@r1concepts.com)
        │  (every 10 min, GitHub Actions cron)
        ▼
  Python agent  ──reads──►  Filters tab (Google Sheet)
        │                            ▲
        │  scores w/ Claude          │ writes
        ▼                            │
  Google Drive    Candidates tab     │
   (3 folders)    (dashboard)        │
                                     │
                            GitHub Pages
                          (HR signs in,
                           edits filters)
```

State is kept in a Gmail label (`resume-bot/processed`), not a database. Re-runs are idempotent.

## Repo layout

```
.
├── .github/workflows/
│   ├── run.yml          # bot cron (every 10 min) + manual trigger
│   └── pages.yml        # deploys /docs to GitHub Pages on each push
├── src/                 # Python agent
│   ├── main.py          # orchestrator
│   ├── config.py        # env-var loading
│   ├── google_auth.py   # service account + DWD
│   ├── gmail_client.py  # list/fetch messages, apply labels
│   ├── drive_client.py  # upload files
│   ├── sheets_client.py # read filters, append dashboard rows
│   ├── resume_parser.py # PDF/DOCX text extraction + OCR fallback
│   └── scorer.py        # Claude scoring
├── docs/                # GitHub Pages filter editor (served at /<owner>.github.io/<repo>/)
│   ├── index.html       # the app
│   └── config.js        # OAuth client ID + Sheet ID (public, commit this)
├── filters.yaml         # seed filters if the Sheet's Filters tab is empty
├── requirements.txt
├── .env.example
├── .gitignore
└── filter-editor-mockup.html   # standalone mockup, for reference only
```

## One-time setup

### 1. Create the Google Cloud project

1. Go to https://console.cloud.google.com → create a project (e.g. `r1-resume-bot`).
2. APIs & Services → Library → enable:
   - **Gmail API**
   - **Google Drive API**
   - **Google Sheets API**

### 2. Create the "backend" OAuth client (for the bot)

The bot authenticates via an OAuth refresh token tied to a one-time sign-in as `jobs@r1concepts.com`. No service account, no Workspace admin needed.

1. APIs & Services → Credentials → Create credentials → OAuth client ID.
2. Application type: **Web application**.
3. Name: `Resume Bot Backend`.
4. Under **Authorized redirect URIs**, add: `https://developers.google.com/oauthplayground`
5. Create. Copy the **Client ID** and **Client Secret** that appear in the dialog. You'll need both in the next step.

### 3. Get the refresh token (one-time, via OAuth Playground)

1. Open https://developers.google.com/oauthplayground in a private/incognito window.
2. Click the gear icon in the top-right → check **Use your own OAuth credentials** → paste the Client ID and Client Secret from step 2 → close the settings.
3. In the left panel, in the **Input your own scopes** box at the bottom of the scope list, paste these three scopes (space-separated):
   ```
   https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/spreadsheets
   ```
4. Click **Authorize APIs**. Sign in as **`jobs@r1concepts.com`** (this is the one and only time anyone needs to know that account's password). Grant the requested permissions.
5. You'll be redirected back to the Playground. Click **Exchange authorization code for tokens**.
6. Copy the **Refresh token** that appears. It looks like `1//0abc...xyz`. Save it somewhere private — you'll paste it into a GitHub secret in step 8.

If at any point you want to revoke this access: go to https://myaccount.google.com/permissions while signed in as jobs@ and remove the "Resume Bot Backend" app. The refresh token will immediately stop working.

### 4. Create an OAuth client ID (for the web UI)

1. APIs & Services → Credentials → Create credentials → OAuth client ID → Web application.
2. Authorized JavaScript origins: `https://<owner>.github.io` (e.g. `https://r1concepts.github.io`).
3. No redirect URI needed (the page uses the implicit token flow).
4. Copy the Client ID — it ends in `.apps.googleusercontent.com`. You'll paste it into `docs/config.js` in step 9.

### 5. Create the Drive folders

In `jobs@r1concepts.com`'s Drive (or wherever HR wants them), create:

- `Resumes / Incoming Resumes` (optional — not used by bot, just a target for the original Gmail-to-Drive forwarding if you set that up separately)
- `Resumes / Processed - Qualified`
- `Resumes / Processed - Not Qualified`
- `Resumes / Needs Further Review`
- `Resumes / Archive / Contacted Candidates` (bot never touches)

For each of the three middle folders, right-click → Share → add the service account's email as **Editor**.

Grab each folder's ID from its URL: `https://drive.google.com/drive/folders/<THIS_IS_THE_ID>`.

### 6. Create the Google Sheet (filters + dashboard)

Create a new Sheet called something like **HR Candidate Review**. Add two tabs:

- **Filters** (used by both the bot and the web UI)
  Row 1 headers: `Role | Minimum requirement | Job hopping rule | Active`
- **Candidates** (the dashboard — the bot creates the header row on first run)

Share the Sheet with:
- The service account email (Editor)
- The HR team Google group, or each HR user individually, as Editor — they need access to use the web UI.

Copy the Sheet ID from the URL: `https://docs.google.com/spreadsheets/d/<THIS_IS_THE_ID>/edit`.

### 7. Push this repo to GitHub

1. Create a new private repo on GitHub (e.g. `resume-bot`).
2. Push this folder as the repo root.

### 8. Add GitHub Actions secrets

Repo Settings → Secrets and variables → Actions → New repository secret. Add each:

| Secret name | Value |
|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | Client ID from step 2 (ends in `.apps.googleusercontent.com`) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Client Secret from step 2 |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | Refresh token from step 3 (starts with `1//`) |
| `GMAIL_USER` | `jobs@r1concepts.com` |
| `ANTHROPIC_API_KEY` | Your Anthropic API key (`sk-ant-...`) |
| `DRIVE_FOLDER_QUALIFIED` | Folder ID from step 5 |
| `DRIVE_FOLDER_NOT_QUALIFIED` | Folder ID from step 5 |
| `DRIVE_FOLDER_REVIEW` | Folder ID from step 5 |
| `DRIVE_FOLDER_INCOMING` | Folder ID from step 5 (optional; leave empty if you didn't make one) |
| `SHEET_ID` | Sheet ID from step 6 |

Optionally, under "Variables" (not Secrets), you can override defaults:

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-sonnet-4-5` | Claude model name |
| `FILTERS_TAB_NAME` | `Filters` | Tab name in Sheet |
| `DASHBOARD_TAB_NAME` | `Candidates` | Tab name in Sheet |
| `MAX_MESSAGES_PER_RUN` | `25` | Safety cap per run |
| `PROCESSED_LABEL` | `resume-bot/processed` | Gmail label name |

### 9. Configure the GitHub Pages web app

Edit `docs/config.js` and replace the two placeholder strings:

```js
window.RESUME_BOT_CONFIG = {
  oauthClientId: "1234567890-xxxx.apps.googleusercontent.com",  // from step 4
  sheetId: "1AbCdEf...XYZ",                                      // from step 6
  filtersTab: "Filters",
  hostedDomain: "r1concepts.com",
};
```

Commit and push.

### 10. Turn on GitHub Pages

Repo Settings → Pages → **Source: GitHub Actions** (not "Deploy from branch"). Save. The next push to `main` that touches `/docs` will deploy it. The URL appears at the top of the Pages settings page.

### 11. First run

In the Actions tab, the **Resume bot** workflow will trigger every 10 minutes automatically. You can also click **Run workflow** to trigger it manually for the first test. Watch the logs; you'll see how many messages it found and what each one was scored as.

## Day-to-day use

- **HR adds/edits filters:** Open the GitHub Pages URL, sign in with their `@r1concepts.com` account, click Add/Edit/Delete on the table. Saves to the Sheet immediately; bot picks up changes on its next run.
- **HR reads decisions:** Open the Candidates tab in the Google Sheet. AI reasoning, best-fit roles, Drive link, and Gmail link are all there. HR fills in their own `HR Status` and `HR Notes` columns; the bot never overwrites those.
- **HR moves contacted candidates:** Drag the file in Drive from `Processed - Qualified` to `Archive / Contacted Candidates`. Bot doesn't touch the Archive folder.

## Costs

- **GitHub Actions:** ~30-60s per run, every 10 min ≈ 4,300 min/month. Private repos get 2,000 free minutes; budget ~$15/month if you exceed the free tier, or drop to every 30 min and stay free.
- **Anthropic API:** ~$0.005/resume with Sonnet. 200 resumes/month ≈ $1/month.
- **Google APIs, GitHub Pages:** Free for this volume.

## Troubleshooting

- **"Missing required environment variable"** in Actions logs → secret name is misspelled or unset.
- **"invalid_grant" or "unauthorized_client" from Google** → refresh token has been revoked or expired. Repeat step 3 (OAuth Playground) to get a fresh one and update the `GOOGLE_OAUTH_REFRESH_TOKEN` secret.
- **No messages processed** → bot is working but everything in the inbox is already labeled. Apply the label manually to test, or send a test resume.
- **Web UI says "Sign-in failed"** → the GitHub Pages URL isn't in the OAuth client's authorized JavaScript origins.
- **"Sheets read failed: 403"** in browser → the signed-in user doesn't have edit access to the Sheet.

## Future improvements

- **Real-time intake** via Gmail Pub/Sub instead of polling (Derek's "long-term solution" — same architecture, swap the trigger).
- **Direct apply form** on the company site that posts straight to the bot, bypassing Gmail for direct applications.
- **"Test a resume" button** in the web UI (needs a Cloudflare Worker to proxy the Anthropic API key safely from a static page).
- **Dedupe** repeat applicants by email/phone.
