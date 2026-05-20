# Resume Filtering Agent — Implementation Plan

**Goal:** A GitHub-hosted Python agent that pulls resumes from `jobs@r1concepts.com`, scores them against the role criteria with Claude, files them into the right Google Drive folder, and appends a row to a shared "HR Candidate Review" Google Sheet — with no manual inbox monitoring required.

---

## 1. Recommended Google authentication: **Service account + domain-wide delegation**

You're on a Google Workspace domain (`@r1concepts.com`), and the agent must act *as* `jobs@r1concepts.com` on an unattended schedule. That's exactly what service account + domain-wide delegation (DWD) is designed for.

| | Service account + DWD (recommended) | OAuth refresh token |
|---|---|---|
| Who sets it up | Workspace **super admin** (one-time) | Anyone with access to the inbox |
| Re-auth needed | Never | Refresh tokens can expire after 6 months of inactivity or password changes |
| Audit trail | Clean — actions logged as service account on behalf of user | Logged as the human user |
| Best for | Production, unattended bots | Quick proofs of concept |

**Action needed from you:** Confirm you can get IT / Workspace admin (likely Eva or whoever owns Google Workspace at R1) to enable domain-wide delegation for one service account with these scopes:

```
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/drive.file
https://www.googleapis.com/auth/spreadsheets
```

**Fallback if admin won't cooperate:** OAuth refresh token. I'll structure the code so swapping the credential source is a one-file change.

---

## 2. Architecture

```
                 ┌─────────────────────────────────────────────┐
                 │   GitHub Actions (cron: */10 * * * *)       │
                 │   + manual workflow_dispatch trigger        │
                 └────────────────────┬────────────────────────┘
                                      │
                                      ▼
              ┌───────────────────────────────────────────────┐
              │  Python agent (src/main.py)                   │
              │                                               │
              │  1. Gmail: list messages w/ attachments       │
              │     NOT labeled `resume-bot/processed`        │
              │                                               │
              │  2. For each message:                         │
              │     a. Download PDF / DOCX attachments        │
              │     b. Extract text (PyPDF2/docx → OCR fallback) │
              │     c. Claude scorer → JSON result            │
              │     d. Upload original file to Drive folder   │
              │        based on result.bucket                 │
              │     e. Append row to HR Candidate Review sheet │
              │     f. Apply Gmail label `resume-bot/processed` │
              │                                               │
              │  3. Log summary, exit                         │
              └───────────────────────────────────────────────┘
                                      │
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
        Google Drive            Gmail label              Google Sheet
        (4 folders)             (idempotency)            (live dashboard)
```

**Key design choice — state is in Gmail labels, not a database.** Applying a `resume-bot/processed` label to handled messages makes the agent fully idempotent (re-runs are safe), gives HR a visible audit trail in Gmail, and avoids running a database. Each run picks up where the last left off via the Gmail search query `has:attachment -label:resume-bot/processed`.

---

## 3. Repository layout

```
resume-bot/
├── .github/
│   └── workflows/
│       └── run.yml                 # cron + workflow_dispatch
├── src/
│   ├── main.py                     # orchestrator
│   ├── config.py                   # env-var loading, folder IDs
│   ├── gmail_client.py             # list/fetch messages, apply labels
│   ├── drive_client.py             # upload file to a folder
│   ├── sheets_client.py            # append row to dashboard
│   ├── resume_parser.py            # PDF + DOCX → plain text (with OCR fallback)
│   ├── scorer.py                   # Claude prompt + structured JSON output
│   └── filters.py                  # loads filters.yaml
├── filters.yaml                    # role criteria (mirrors your Excel)
├── tests/
│   └── test_scorer.py              # golden-resume test cases
├── requirements.txt
├── .env.example
└── README.md                       # setup instructions
```

---

## 4. How the Claude scorer works

For each resume, the agent sends Claude:
- The full extracted resume text
- The 4 role criteria from your `Resume Filters.xlsx`
- Instructions to return strict JSON

Expected JSON response:

```json
{
  "bucket": "qualified" | "not_qualified" | "needs_review",
  "best_fit_roles": ["Cherry Picker", "Cycle Count"],
  "years_relevant_experience": 2.5,
  "job_hopping_flag": "positive" | "caution" | "neutral",
  "reasoning": "Has 3 years cherry picker experience at XYZ Logistics (2022-2025) and 1 year at ABC Warehouse...",
  "confidence": 0.85,
  "candidate_name": "Jane Doe",
  "candidate_email": "jane@example.com",
  "candidate_phone": "555-1234"
}
```

**Routing logic:**
- `qualified` → `Processed - Qualified` folder
- `not_qualified` → `Processed - Not Qualified` folder
- `needs_review` (low confidence or ambiguous) → `Needs Further Review` folder
- Archive folder is never touched by the bot (HR moves files there manually)

The `reasoning` and `best_fit_roles` fields go straight into the dashboard, which directly addresses Eva's two requested columns.

---

## 5. The "HR Candidate Review" dashboard (Google Sheet)

One row per resume, appended in real time. Columns:

| Column | Source |
|---|---|
| Timestamp | Run time |
| Candidate Name | Claude extraction |
| Email | Claude extraction |
| Phone | Claude extraction |
| Original Filename | Gmail attachment name |
| Best-Fit Role(s) | Claude `best_fit_roles` |
| Decision | Claude `bucket` |
| Years Relevant Exp | Claude `years_relevant_experience` |
| Job Hopping | Claude `job_hopping_flag` |
| Confidence | Claude `confidence` |
| AI Reasoning | Claude `reasoning` |
| Drive File Link | Direct link to the file in its destination folder |
| Gmail Thread Link | Direct link back to the original email |
| HR Status | Blank — HR fills this (Contacted / Interviewing / Hired / Rejected) |
| HR Notes | Blank — HR fills this |

The two HR-managed columns (`HR Status`, `HR Notes`) stay untouched by the bot on subsequent runs, so HR can work in the sheet without losing their notes.

---

## 6. Setup steps (one-time)

### 6a. Google Workspace / GCP
1. Create a GCP project (e.g., `r1-resume-bot`).
2. Enable APIs: Gmail, Drive, Sheets.
3. Create a service account (e.g., `resume-bot@r1-resume-bot.iam.gserviceaccount.com`), download JSON key.
4. Workspace admin: enable domain-wide delegation for that service account's client ID with the 3 scopes listed in §1.
5. Create the Drive folder tree (you already did this — just need the folder IDs from each URL).
6. Create the Google Sheet "HR Candidate Review" with the column headers from §5; capture the sheet ID.
7. Share the 4 Drive folders + the Sheet with the service account email as Editor (belt-and-suspenders alongside DWD).

### 6b. GitHub
1. Create a private repo `resume-bot` and push the code I'll generate.
2. Add these repo Secrets (Settings → Secrets → Actions):
   - `GOOGLE_SA_JSON` — paste full service account JSON
   - `ANTHROPIC_API_KEY` — your Claude API key
   - `GMAIL_USER` — `jobs@r1concepts.com`
   - `DRIVE_FOLDER_QUALIFIED` — folder ID
   - `DRIVE_FOLDER_NOT_QUALIFIED` — folder ID
   - `DRIVE_FOLDER_REVIEW` — folder ID
   - `DRIVE_FOLDER_INCOMING` — folder ID (optional, for archival copies)
   - `SHEET_ID` — dashboard sheet ID
3. The included workflow turns Actions on automatically. Watch the first run in the Actions tab.

---

## 7. Risks, edge cases, and costs

**Image-based PDFs (scans).** Some applicants submit scanned PDFs with no text layer. The parser tries `pypdf` first, falls back to OCR via Tesseract (pre-installed on `ubuntu-latest` runners with one `apt-get` step in the workflow). OCR'd text is noisier but workable; the scorer prompt includes "if the resume is OCR'd and unclear, default to `needs_review`."

**Forwarded emails / nested attachments.** Some resumes arrive as forwarded messages with the PDF inside. The Gmail client recursively walks message parts to find all PDF/DOCX attachments at any nesting level.

**Duplicate applicants.** Same person applies twice. Dashboard will show two rows; you'll see the duplicate from name/email. A future enhancement can dedupe in the sheet, but I'd leave it visible at first so HR sees re-applications.

**Cost (Claude Sonnet, ~2-page resume).** ~$0.005 per resume. 200 resumes/month ≈ $1/month. Anthropic API has no minimums.

**GitHub Actions cost.** Each run is ~30-60s. At every-10-min cadence: ~4,300 min/month. Private repos get 2,000 free minutes; budget ~$15/month if you exceed, or drop to every 30 min and stay free.

**Failure modes & visibility.** If a run fails (API outage, bad PDF, etc.) the unprocessed message stays unlabeled and gets retried on the next run. The workflow emails the repo owner on failure (built-in GitHub feature). Optional: post a Slack message on failure.

**PII / data handling.** Resumes touch (a) GitHub Actions ephemeral runners (destroyed after each run, no persisted artifacts unless we explicitly upload them), (b) the Anthropic API (not used for training when called via API per Anthropic's commercial terms), and (c) Google Workspace (already where the data lives). Worth confirming with whoever owns compliance at R1 before flipping production on.

**Long-term path Derek mentioned.** The "apply directly for role X" autonomous pipeline is the same architecture with one more step: a public-facing apply form posts to the bot's intake instead of (or alongside) the Gmail trigger. The Gmail intake stays as the human-friendly fallback. Nothing in this design needs to change to support that — just add an HTTP entry point later.

---

## 8. Open questions for you before I build

1. **Domain-wide delegation:** Can you (or someone) get a Workspace admin to enable it? If "definitely not," I'll wire OAuth refresh-token auth instead.
2. **Drive folder IDs and Sheet ID:** I can build with placeholders, but you'll need to plug them in before the first run.
3. **Run cadence:** Every 10 minutes feels right for HR — fast enough that the dashboard feels live, slow enough to stay in the free GitHub Actions tier. Adjust?
4. **Slack/email notification on failure:** Want me to wire one up, or rely on GitHub's default failure email to the repo owner?
5. **Filter editing in the future:** Eva asked about adding filters later. I'll put role criteria in `filters.yaml` (human-readable) so non-engineers can edit it via a PR or a direct edit on GitHub. Sound good, or do you want it loaded from a Google Sheet so HR can edit without touching GitHub?
