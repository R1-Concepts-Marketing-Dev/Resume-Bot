"""One-off Candidates dashboard migration (2026-06-18).

Transforms the Candidates tab from the old 20-21 column layout to the
new 18-column layout with Application Submitted at column E:

  OLD (21 cols, A:U):
    A Timestamp | B Name | C Email | D Phone | E Filename |
    F Applied For | G Cross-Fit Match | H Cross-Fit Flag |
    I Decision | J Years Exp | K Job Hopping | L Confidence |
    M AI Reasoning | N Drive Link | O Gmail Link |
    P HR Status | Q HR Notes |
    R Recruiter/Agency | S Indeed | T Indeed Action Done |
    U Application Submitted

  NEW (18 cols, A:R):
    A Timestamp | B Name | C Email | D Phone |
    E Application Submitted (NEW POSITION) |
    F Filename | G Applied For | H Cross-Fit Match |
    I Cross-Fit Flag | J Decision | K Years Exp |
    L Job Hopping | M Confidence | N AI Reasoning |
    O Drive Link | P Gmail Link | Q HR Status | R HR Notes

Also:
  * Updates every Indeed Queue HR Status VLOOKUP formula from
    Candidates!A:P,16 -> Candidates!A:Q,17
  * Backfills Indeed Queue for any Candidates row where the new E
    column = "Indeed" but no matching timestamp exists in Indeed Queue.

Trigger from GitHub Actions: workflow_dispatch on migrate_v2.yml. The
workflow re-uses the bot's existing OAuth secrets. Idempotent -- safe
to re-dispatch.
"""

from __future__ import annotations

import logging
import sys

from . import config, google_auth


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("migrate-v2")


CANDIDATES_TAB = "Candidates"
INDEED_QUEUE_TAB = "Indeed Queue"


def _col_letter(n: int) -> str:
    """1-indexed column letter. 1=A, 26=Z, 27=AA."""
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _get_sheet_id(svc, spreadsheet_id, tab_name):
    meta = svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties",
    ).execute()
    for s in meta.get("sheets", []):
        props = s.get("properties") or {}
        if props.get("title") == tab_name:
            return props.get("sheetId")
    return None


def _compute_application_submitted(row, idx_r, idx_s, idx_u):
    """Compute the Application Submitted value for one row.

    Prefer the existing column U value if present (rows written by the
    post-d1c8c97 bot have it). If U was "Recruiter/Agency" and column R
    has an actual agency name, upgrade to "Recruiter/Agency - <name>".
    Otherwise derive from R (recruiter) and S (Indeed Yes/No)."""
    existing = (str(row[idx_u]).strip() if idx_u is not None and idx_u < len(row) else "")
    recruiter = (str(row[idx_r]).strip() if idx_r is not None and idx_r < len(row) else "")
    indeed_str = (str(row[idx_s]).strip().lower() if idx_s is not None and idx_s < len(row) else "")

    has_recruiter = recruiter and recruiter.lower() not in {
        "n/a", "na", "none", "null", "unknown", "",
    }
    is_indeed = indeed_str == "yes"

    if existing:
        if existing.lower().startswith("recruiter/agency") and has_recruiter \
                and recruiter not in existing:
            return f"Recruiter/Agency - {recruiter}"
        return existing

    if has_recruiter:
        return f"Recruiter/Agency - {recruiter}"
    if is_indeed:
        return "Indeed"
    return "Email"


def migrate_candidates(svc, sheet_id):
    """Step 1: restructure the Candidates tab."""
    inner_id = _get_sheet_id(svc, sheet_id, CANDIDATES_TAB)
    if inner_id is None:
        raise RuntimeError(f"Tab '{CANDIDATES_TAB}' not found")

    # Read everything we need: headers + all rows.
    full = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{CANDIDATES_TAB}!A1:Z",
    ).execute()
    rows = full.get("values", [])
    if not rows:
        log.warning("Candidates is empty. Nothing to migrate.")
        return
    headers = rows[0]
    data = rows[1:]
    log.info("Before: %d data rows, %d cols (headers=%s)",
             len(data), len(headers), headers)

    # Idempotency: if E1 is already "Application Submitted" and total
    # cols == 18, the migration has already run.
    if len(headers) >= 5 and headers[4] == "Application Submitted" \
            and len([h for h in headers if h]) == 18:
        log.info("Migration already complete. Skipping Candidates restructure.")
        return

    # Headers in the live sheet have drifted (extra spaces, junk cells from
    # stale filter-range hints, etc.). Use POSITIONAL indexes from the
    # documented old layout instead of header lookup:
    #   R=col 18 (0-idx 17) = Recruiter/Agency
    #   S=col 19 (0-idx 18) = Indeed
    #   T=col 20 (0-idx 19) = Indeed Action Done
    #   U=col 21 (0-idx 20) = Application Submitted (added recently)
    idx_r = 17 if len(headers) > 17 else None
    idx_s = 18 if len(headers) > 18 else None
    idx_u = 20 if len(headers) > 20 else None
    log.info("Using positional indexes: R=%s S=%s U=%s (headers had %s cols)",
             idx_r, idx_s, idx_u, len(headers))

    if idx_r is None:
        raise RuntimeError(
            f"Candidates only has {len(headers)} cols; expected >=18. Got: {headers}"
        )

    # Compute Application Submitted values for every row.
    app_submitted = [
        _compute_application_submitted(r, idx_r, idx_s, idx_u)
        for r in data
    ]
    log.info("Computed Application Submitted for %d rows. Sample: %s",
             len(app_submitted), app_submitted[:3])

    # --- Step 1a: insert a new column at position E (index 4 zero-based) ---
    # Then write the new header + values.
    requests = [{
        "insertDimension": {
            "range": {
                "sheetId": inner_id,
                "dimension": "COLUMNS",
                "startIndex": 4,
                "endIndex": 5,
            },
            "inheritFromBefore": False,
        }
    }]
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests},
    ).execute()
    log.info("Inserted new column at E.")

    # Write E1 header
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{CANDIDATES_TAB}!E1",
        valueInputOption="RAW",
        body={"values": [["Application Submitted"]]},
    ).execute()

    # Write E2:E{n} values
    if app_submitted:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{CANDIDATES_TAB}!E2:E{1 + len(app_submitted)}",
            valueInputOption="USER_ENTERED",
            body={"values": [[v] for v in app_submitted]},
        ).execute()
    log.info("Wrote Application Submitted to E1:E%d", 1 + len(app_submitted))

    # --- Step 1b: delete trailing R/S/T/U columns ---
    # After the insert, total cols = old + 1. We want exactly 18.
    # Re-fetch header to get the post-insert total.
    new_full = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{CANDIDATES_TAB}!A1:Z1",
    ).execute()
    new_headers = (new_full.get("values") or [[]])[0]
    log.info("Post-insert headers (%d): %s", len(new_headers), new_headers)

    new_last_col = len(new_headers)
    cols_to_delete = new_last_col - 18
    if cols_to_delete > 0:
        # Delete from index 18 (0-based) to index new_last_col
        del_requests = [{
            "deleteDimension": {
                "range": {
                    "sheetId": inner_id,
                    "dimension": "COLUMNS",
                    "startIndex": 18,
                    "endIndex": 18 + cols_to_delete,
                }
            }
        }]
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": del_requests},
        ).execute()
        log.info("Deleted %d trailing columns.", cols_to_delete)

    # Verify
    verify = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{CANDIDATES_TAB}!A1:R1",
    ).execute()
    verify_headers = (verify.get("values") or [[]])[0]
    log.info("After: %d cols. Final headers: %s",
             len(verify_headers), verify_headers)


def fix_indeed_queue_formulas(svc, sheet_id):
    """Step 2: rewrite Indeed Queue HR Status VLOOKUP formulas to point
    at the new HR Status position (Candidates!A:Q col 17)."""
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{INDEED_QUEUE_TAB}!E2:E",
            valueRenderOption="FORMULA",
        ).execute()
    except Exception as e:
        log.warning("Could not read Indeed Queue formulas: %s", e)
        return
    formulas = resp.get("values", []) or []
    if not formulas:
        log.info("Indeed Queue is empty. Nothing to update.")
        return

    updated = 0
    skipped = 0
    new_formulas = []
    for row in formulas:
        f = (row[0] if row else "") or ""
        if not f:
            new_formulas.append([f])
            continue
        if "Candidates!A:Q,17" in f:
            skipped += 1
            new_formulas.append([f])
            continue
        fixed = f.replace("Candidates!A:P,16", "Candidates!A:Q,17")
        if fixed != f:
            updated += 1
        new_formulas.append([fixed])

    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{INDEED_QUEUE_TAB}!E2:E{1 + len(new_formulas)}",
        valueInputOption="USER_ENTERED",
        body={"values": new_formulas},
    ).execute()
    log.info("Indeed Queue formulas: updated=%d already-correct=%d",
             updated, skipped)


# Decision -> Fit Quality / AI Recommendation (mirror sheets_client.py)
_FIT_QUALITY = {
    "qualified":      "Strong",
    "needs_review":   "Needs review",
    "not_qualified":  "Not a fit",
    "pending_paused": "Hold - role paused",
    "unreadable":     "Unreadable resume",
}
_AI_RECOMMENDATION = {
    "qualified":      "Move to interview stage",
    "needs_review":   "Review resume + decide",
    "not_qualified":  "Decline / Not a fit",
    "pending_paused": "Hold - role paused",
    "unreadable":     "Review manually",
}


def _is_indeed_candidate(row):
    """Detect whether a Candidates row came from Indeed using multiple
    signals (the old Indeed column was unreliable -- many rows had it
    blank). Signals (any one is enough):
      * E (Application Submitted) starts with "Indeed"
      * F (filename) contains "indeed" (e.g. indeed_quick_apply_resume.pdf)
      * N (AI Reasoning) mentions an indeed.com sender
      * P (Gmail Thread Link) -- can't introspect URL alone
    """
    appsub = str(row[4]).strip().lower() if len(row) > 4 else ""
    filename = str(row[5]).strip().lower() if len(row) > 5 else ""
    reasoning = str(row[13]).lower() if len(row) > 13 else ""

    if appsub.startswith("indeed"):
        return True
    if "indeed" in filename:
        return True
    # Reasoning sometimes mentions the sender; e.g. "applied via indeed.com"
    if "indeed.com" in reasoning or "indeed quick apply" in reasoning:
        return True
    return False


def backfill_indeed_queue(svc, sheet_id):
    """Step 3: walk Candidates after migration, and for every row that
    looks like an Indeed candidate (per _is_indeed_candidate -- uses
    multiple signals because the old Indeed column was unreliable) and
    no matching timestamp exists in Indeed Queue, append a row.

    Also upgrades the Candidates Application Submitted cell to "Indeed"
    if we detected Indeed via signal other than appsub (the field was
    stale at migration time)."""
    cand_resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{CANDIDATES_TAB}!A2:R",
    ).execute()
    cand_rows = cand_resp.get("values", []) or []
    if not cand_rows:
        log.info("Candidates is empty. Nothing to backfill.")
        return

    # Existing Indeed Queue timestamps (col G).
    queue_resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{INDEED_QUEUE_TAB}!G2:G",
    ).execute()
    existing_ts = set()
    for r in queue_resp.get("values", []) or []:
        if r and r[0]:
            existing_ts.add(str(r[0]).strip())
    log.info("Existing Indeed Queue timestamps: %d", len(existing_ts))

    to_append = []
    appsub_upgrades = []  # (row_number, new_value)
    for i, r in enumerate(cand_rows, start=2):  # row 1 is header, data starts at 2
        r = (r + [""] * 18)[:18]
        ts = str(r[0]).strip()
        if not ts:
            continue
        if not _is_indeed_candidate(r):
            continue

        # Upgrade Application Submitted if it was previously misclassified.
        current_appsub = str(r[4]).strip()
        if not current_appsub.lower().startswith("indeed"):
            appsub_upgrades.append((i, "Indeed"))

        if ts in existing_ts:
            continue

        name = str(r[1]).strip()
        applied = str(r[6]).strip()
        decision = str(r[9]).strip()
        fit = _FIT_QUALITY.get(decision, decision)
        rec = _AI_RECOMMENDATION.get(decision, "Review manually")
        hr_formula = (
            f'=IFERROR(VLOOKUP("{ts}",Candidates!A:Q,17,FALSE),"")'
        )
        to_append.append([name, applied, fit, rec, hr_formula, False, ts])

    # Apply Application Submitted upgrades in one batchUpdate.
    if appsub_upgrades:
        update_data = [
            {"range": f"{CANDIDATES_TAB}!E{row_num}",
             "values": [[val]]}
            for row_num, val in appsub_upgrades
        ]
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"data": update_data, "valueInputOption": "USER_ENTERED"},
        ).execute()
        log.info("Upgraded Application Submitted to Indeed for %d rows.",
                 len(appsub_upgrades))

    if not to_append:
        log.info("Indeed Queue backfill: nothing to add (%d rows examined).",
                 len(cand_rows))
        return

    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{INDEED_QUEUE_TAB}!A:G",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": to_append},
    ).execute()
    log.info("Backfilled %d rows into Indeed Queue.", len(to_append))


# ============================================================
# Email backfill: extract from resume PDFs on Drive for rows
# where the Email column is blank.
# ============================================================
#
# When the bot landed rows where the scorer couldn't extract a
# candidate email and the sender was a job-board alias, column C ended
# up empty. For those rows we still have the resume PDF on Drive.
# This backfill walks each blank-email row, downloads the PDF, extracts
# text, regex-matches an email, and writes it back if found. Pure regex
# (no LLM cost) -- a real email in the resume body is almost always
# easy to spot once we have the text.

import re as _re

_EMAIL_REGEX = _re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_JOB_BOARD_DOMAINS = (
    "indeed.com", "indeedemail.com", "ziprecruiter.com",
    "glassdoor.com", "monster.com", "careerbuilder.com", "snagajob.com",
    "r1concepts.com",  # don't suggest internal addrs
)


def _is_job_board_email(addr: str) -> bool:
    addr = (addr or "").lower()
    if "@" not in addr:
        return False
    domain = addr.rsplit("@", 1)[-1]
    return any(domain == d or domain.endswith("." + d) for d in _JOB_BOARD_DOMAINS)


def _extract_email_from_drive(drive_svc, drive_cell: str) -> str:
    """drive_cell is the formula =HYPERLINK("https://drive.google.com/.../file_id/...","Link").
    Returns the first non-job-board email found in the PDF, or "" on
    any failure. Quietly degrades on errors so one bad row doesn't kill
    the whole backfill pass."""
    if not drive_cell:
        return ""
    m = _re.search(r"/d/([A-Za-z0-9_-]{20,})", drive_cell)
    if not m:
        return ""
    file_id = m.group(1)
    try:
        meta = drive_svc.files().get(fileId=file_id, fields="mimeType").execute()
        mime = meta.get("mimeType", "")
        data = drive_svc.files().get_media(fileId=file_id).execute()
    except Exception as e:
        log.warning("Drive read failed for %s: %s", file_id, e)
        return ""
    try:
        from . import resume_parser
        text, _ = resume_parser.extract("resume", mime, data)
    except Exception as e:
        log.warning("resume_parser.extract failed: %s", e)
        return ""
    if not text:
        return ""
    for match in _EMAIL_REGEX.findall(text):
        addr = match.strip().rstrip(".,;:")
        if not _is_job_board_email(addr):
            return addr
    return ""


def backfill_emails(svc, sheet_id, drive_svc, tab=CANDIDATES_TAB,
                    max_rows: int = 500):
    """Walk Candidates, find rows with blank col C (Email), try to
    extract the candidate's email from the resume PDF on Drive (col O).
    Writes back to col C only if a non-job-board email was found.

    max_rows caps how many empty rows we attempt (each one is a Drive
    + parser call -- the cap keeps a single audit run from taking 30+
    minutes on a 1000-row sheet)."""
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{tab}!A2:R",
        valueRenderOption="FORMULA",
    ).execute()
    rows = resp.get("values", []) or []
    log.info("Email backfill: %d Candidates rows", len(rows))

    attempted = 0
    found = 0
    updates = []
    for i, r in enumerate(rows, start=2):
        r = (r + [""] * 18)[:18]
        email = str(r[2]).strip()
        if email:
            continue  # already has email
        if attempted >= max_rows:
            log.info("Hit max_rows cap (%d); stopping early.", max_rows)
            break
        attempted += 1
        drive_link = str(r[14]).strip()  # col O = Drive File Link
        if not drive_link:
            continue
        extracted = _extract_email_from_drive(drive_svc, drive_link)
        if extracted:
            found += 1
            updates.append({"range": f"{tab}!C{i}",
                            "values": [[extracted]]})
            log.info("Row %d: extracted %s", i, extracted)

    if updates:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"data": updates, "valueInputOption": "USER_ENTERED"},
        ).execute()
    log.info("Email backfill complete: attempted=%d found=%d wrote=%d",
             attempted, found, len(updates))


# ============================================================
# Gmail thread-link backfill (authuser=)
# ============================================================
#
# Prior to commit d1c8c97 (2026-06-18) the bot wrote Gmail thread links as
#   https://mail.google.com/mail/u/0/#inbox/<id>
# That URL routes the click under whichever account the user is signed
# in to FIRST (authuser=0), which for HR users is their work mailbox --
# the thread doesn't exist there, so they hit an empty Gmail page. New
# rows now use ?authuser=jobs@r1concepts.com to force routing to the
# jobs@ mailbox. This step rewrites every old-format link on the
# Candidates tab to the new format so HR clicks work retroactively.

_OLD_LINK_PATTERN = "https://mail.google.com/mail/u/0/#inbox/"
_NEW_LINK_PREFIX = "https://mail.google.com/mail/?authuser=jobs@r1concepts.com#inbox/"


def backfill_gmail_links(svc, sheet_id, tab=CANDIDATES_TAB, col_letter="P"):
    """Rewrite =HYPERLINK("<old-url>","Link") formulas in column P to
    use the new authuser-pinned URL. Idempotent: skips formulas already
    on the new format."""
    rng = f"{tab}!{col_letter}2:{col_letter}"
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=rng,
            valueRenderOption="FORMULA",
        ).execute()
    except Exception as e:
        log.warning("Could not read %s Gmail links: %s", tab, e)
        return
    rows = resp.get("values", []) or []
    if not rows:
        log.info("%s: no Gmail link rows to backfill.", tab)
        return

    updated = 0
    skipped = 0
    new_rows = []
    for r in rows:
        cell = (r[0] if r else "") or ""
        if not cell:
            new_rows.append([cell])
            continue
        if _OLD_LINK_PATTERN in cell:
            new_cell = cell.replace(_OLD_LINK_PATTERN, _NEW_LINK_PREFIX)
            new_rows.append([new_cell])
            updated += 1
        else:
            skipped += 1
            new_rows.append([cell])

    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{tab}!{col_letter}2:{col_letter}{1 + len(new_rows)}",
        valueInputOption="USER_ENTERED",
        body={"values": new_rows},
    ).execute()
    log.info("Gmail link backfill on %s col %s: updated=%d unchanged=%d",
             tab, col_letter, updated, skipped)


def inspect_row(svc, sheet_id, tab, row_number):
    """Dump one Candidates row's contents to the log for diagnosis.
    row_number is 1-indexed including the header row (so row 13 in the
    sheet = row_number=13)."""
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab}!A{row_number}:R{row_number}",
            valueRenderOption="FORMULA",
        ).execute()
    except Exception as e:
        log.warning("Could not read row %d: %s", row_number, e)
        return
    row = (resp.get("values") or [[]])[0]
    headers = ["A_Timestamp","B_Name","C_Email","D_Phone","E_AppSub",
               "F_Filename","G_AppliedFor","H_CrossFitMatch","I_CrossFitFlag",
               "J_Decision","K_YearsExp","L_JobHopping","M_Confidence",
               "N_AIReasoning","O_DriveLink","P_GmailLink","Q_HRStatus","R_HRNotes"]
    log.info("--- Candidates row %d dump ---", row_number)
    for i, h in enumerate(headers):
        val = row[i] if i < len(row) else ""
        # Truncate AI Reasoning so logs stay readable.
        if h == "N_AIReasoning" and len(str(val)) > 200:
            val = str(val)[:200] + "..."
        log.info("  %s: %r", h, val)


def run() -> int:
    cfg = config.load()
    log.info("Migration starting. Sheet=%s", cfg.sheet_id)
    creds = google_auth.make_credentials(
        cfg.oauth_client_id, cfg.oauth_client_secret, cfg.oauth_refresh_token
    )
    svc = google_auth.sheets(creds)

    log.info("STEP 1: Candidates tab restructure")
    migrate_candidates(svc, cfg.sheet_id)

    log.info("STEP 2: Indeed Queue formula update")
    fix_indeed_queue_formulas(svc, cfg.sheet_id)

    log.info("STEP 3: Indeed Queue backfill")
    backfill_indeed_queue(svc, cfg.sheet_id)

    log.info("STEP 4: Gmail link backfill (authuser=jobs@)")
    backfill_gmail_links(svc, cfg.sheet_id)

    log.info("STEP 5: Email backfill (extract from resume PDFs on Drive)")
    drive_svc = google_auth.drive(creds)
    backfill_emails(svc, cfg.sheet_id, drive_svc)

    log.info("STEP 6: Diagnostic dump of rows 13 and 26 for review")
    inspect_row(svc, cfg.sheet_id, CANDIDATES_TAB, 13)
    inspect_row(svc, cfg.sheet_id, CANDIDATES_TAB, 26)

    log.info("Migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
