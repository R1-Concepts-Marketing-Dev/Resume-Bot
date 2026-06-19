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
    """Step 2: FORCE-REWRITE Indeed Queue HR Status formulas based on the
    timestamp in column G of each row. This is intentionally aggressive
    because rows can get corrupted in various ways: stale formulas
    pointing at the old col P/16, accidental HYPERLINK formulas
    (resulting in the cell displaying "Link"), manual edits, blank
    cells, etc. We don't try to detect "is the current value correct";
    we just write the right formula for every row that has a timestamp.
    """
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{INDEED_QUEUE_TAB}!A2:G",
        ).execute()
    except Exception as e:
        log.warning("Could not read Indeed Queue rows: %s", e)
        return
    rows = resp.get("values", []) or []
    if not rows:
        log.info("Indeed Queue is empty. Nothing to update.")
        return

    new_formulas = []
    written = 0
    blanked = 0
    for r in rows:
        # Col G (index 6) holds the timestamp join key.
        ts = (r[6] if len(r) > 6 else "") or ""
        ts = str(ts).strip()
        if not ts:
            # No timestamp -> nothing to VLOOKUP; leave the cell blank
            # instead of writing a formula that would always return "".
            new_formulas.append([""])
            blanked += 1
            continue
        new_formulas.append([
            f'=IFERROR(VLOOKUP("{ts}",Candidates!A:Q,17,FALSE),"")'
        ])
        written += 1

    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{INDEED_QUEUE_TAB}!E2:E{1 + len(new_formulas)}",
        valueInputOption="USER_ENTERED",
        body={"values": new_formulas},
    ).execute()
    log.info("Indeed Queue HR Status: force-wrote %d formulas, %d rows blank (no timestamp).",
             written, blanked)


def fix_cross_match_query(svc, sheet_id, tab="Cross-Match"):
    """Rewrite the Cross-Match QUERY in A2 to match the user's existing
    row 1 headers exactly:

      A Timestamp | B Applied For | C Better Match | D Candidate N |
      E Email | F Phone | G Confidence | H AI Reasoning |
      I Drive | J Gmail | K HR Status | L HR Notes

    Mapping (Cross-Match col -> Candidates col):
      A Timestamp     <- Candidates!A
      B Applied For   <- Candidates!G
      C Better Match  <- Candidates!H (Cross-Fit Match)
      D Candidate N   <- Candidates!B
      E Email         <- Candidates!C
      F Phone         <- Candidates!D
      G Confidence    <- Candidates!M
      H AI Reasoning  <- Candidates!N
      I Drive         <- Candidates!O
      J Gmail         <- Candidates!P
      K HR Status     <- Candidates!Q
      L HR Notes      <- Candidates!R

    Filter: rows where Cross-Fit Flag (Candidates!I) is the rocket emoji
    AND HR Status (Candidates!Q) is empty (HR hasn't actioned yet).
    Ordered by Timestamp DESC. Uses header_count=0 so QUERY does NOT
    inject its own header row -- the user's row 1 manual headers are
    the only headers."""
    try:
        meta = svc.spreadsheets().get(
            spreadsheetId=sheet_id,
            fields="sheets.properties.title",
        ).execute()
    except Exception as e:
        log.warning("Could not load sheet metadata: %s", e)
        return
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])
              if s.get("properties")]
    if tab not in titles:
        log.info("No %s tab; skipping cross-match QUERY fix.", tab)
        return

    # Build QUERY that maps Candidates cols A,G,H,B,C,D,M,N,O,P,Q,R
    # in the order that matches the user's 12-col Cross-Match header row.
    # header_count=0 -- no extra header row added by QUERY.
    rocket = "\U0001F6A8"  # 🚨
    new_query = (
        '=IFERROR(QUERY(Candidates!A2:R, '
        '"SELECT A, G, H, B, C, D, M, N, O, P, Q, R '
        'WHERE I = \'' + rocket + '\' '
        'AND (Q IS NULL OR Q = \'\') '
        'ORDER BY A DESC", 0), "")'
    )

    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{tab}!A2",
        valueInputOption="USER_ENTERED",
        body={"values": [[new_query]]},
    ).execute()
    log.info("%s!A2 QUERY rewritten -- 12 columns matching user headers.", tab)


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


def enrich_indeed_from_gmail(svc, sheet_id, gmail_svc, gmail_user,
                             tab=CANDIDATES_TAB):
    """For Candidates rows whose Application Submitted is not yet
    "Indeed", look up the row's Gmail thread by its embedded thread_id
    (in col P) and check whether the original sender domain is
    indeed.com / indeedemail.com / similar. If so, mark the row as
    Indeed so the subsequent backfill_indeed_queue picks it up.

    Quietly skips rows where the thread fetch fails (deleted threads,
    permission issues, etc.)."""
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{tab}!A2:R",
        valueRenderOption="FORMULA",
    ).execute()
    rows = resp.get("values", []) or []
    if not rows:
        return

    job_board_domains = (
        "indeed.com", "indeedemail.com", "ziprecruiter.com",
        "glassdoor.com", "monster.com", "careerbuilder.com", "snagajob.com",
    )

    examined = 0
    upgraded = 0
    failed = 0
    updates = []
    for i, r in enumerate(rows, start=2):
        r = (r + [""] * 18)[:18]
        appsub = str(r[4]).strip().lower()
        if appsub.startswith("indeed"):
            continue  # already correctly labeled
        gmail_cell = str(r[15]).strip()  # col P
        tm = _re.search(r"#inbox/([A-Za-z0-9_-]+)", gmail_cell)
        if not tm:
            continue
        thread_id = tm.group(1)
        examined += 1
        if examined > 200:
            break  # cap API calls per run
        try:
            thread = gmail_svc.users().threads().get(
                userId=gmail_user, id=thread_id,
                format="metadata", metadataHeaders=["From"],
            ).execute()
        except Exception:
            failed += 1
            continue
        from_addr = ""
        for msg in thread.get("messages", []):
            for h in msg.get("payload", {}).get("headers", []):
                if h.get("name", "").lower() == "from":
                    from_addr = h.get("value", "")
                    break
            if from_addr:
                break
        if not from_addr:
            continue
        # Normalize: extract domain
        import email.utils as _eu
        _, sender_email = _eu.parseaddr(from_addr)
        if "@" not in sender_email:
            continue
        domain = sender_email.rsplit("@", 1)[-1].lower()
        if any(domain == d or domain.endswith("." + d) for d in job_board_domains):
            updates.append({"range": f"{tab}!E{i}",
                            "values": [["Indeed"]]})
            upgraded += 1

    if updates:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"data": updates, "valueInputOption": "USER_ENTERED"},
        ).execute()
    log.info("Gmail Indeed enrichment: examined=%d upgraded=%d failed=%d",
             examined, upgraded, failed)


# Decision -> Fit Quality and AI Recommendation lookups.
# Used by backfill_indeed_queue and backfill_indeed_queue_columns.
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


def rebuild_indeed_queue(svc, sheet_id):
    """Nuclear rebuild of the Indeed Queue tab. Schema-drift has put
    wrong-shaped data in B/C, blank headers in B/F, and a bunch of
    duplicate (name, empty-timestamp) rows that the previous backfill
    couldn't clean up correctly.

    This wipes all data rows (keeping row 1 only), rewrites the canonical
    7-column header, then re-appends one row per Indeed candidate from
    the Candidates tab.

    Canonical schema (col A through G):
      A Candidate Name | B Position | C Fit Quality |
      D AI Recommendation | E HR Status (VLOOKUP) |
      F Indeed Application Closed (FALSE checkbox) |
      G Timestamp (join key)

    HR's edits to the Indeed Application Closed checkbox are wiped --
    if any rows were checked, they'll go back to FALSE. That column
    is HR's working state; we're trading one-time loss for a clean
    schema."""
    inner_id = _get_sheet_id(svc, sheet_id, INDEED_QUEUE_TAB)
    if inner_id is None:
        log.warning("Indeed Queue tab not found")
        return

    # 1. Read Candidates to figure out what rows to write.
    cand_resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{CANDIDATES_TAB}!A2:R",
    ).execute()
    cand_rows = cand_resp.get("values", []) or []
    indeed_rows = []
    for r in cand_rows:
        r = (r + [""] * 18)[:18]
        ts = str(r[0]).strip()
        if not ts:
            continue
        if not _is_indeed_candidate(r):
            continue
        name = str(r[1]).strip()
        applied = str(r[6]).strip()
        decision = str(r[9]).strip()
        fit = _FIT_QUALITY.get(decision, decision)
        rec = _AI_RECOMMENDATION.get(decision, "Review manually")
        hr_formula = (
            f'=IFERROR(VLOOKUP("{ts}",Candidates!A:Q,17,FALSE),"")'
        )
        indeed_rows.append([name, applied, fit, rec, hr_formula, False, ts])
    log.info("Rebuild plan: %d Indeed candidates from Candidates",
             len(indeed_rows))

    # 2. Clear all data rows on Indeed Queue (keep row 1 for header).
    svc.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=f"{INDEED_QUEUE_TAB}!A2:Z",
    ).execute()
    log.info("Cleared Indeed Queue data rows.")

    # 3. Rewrite the canonical 7-column header.
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{INDEED_QUEUE_TAB}!A1:G1",
        valueInputOption="RAW",
        body={"values": [[
            "Candidate Name", "Position", "Fit Quality",
            "AI Recommendation", "HR Status",
            "Indeed Application Closed", "Timestamp",
        ]]},
    ).execute()
    log.info("Rewrote Indeed Queue headers.")

    # 4. Append the new rows.
    if indeed_rows:
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{INDEED_QUEUE_TAB}!A2",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": indeed_rows},
        ).execute()
        # Make sure the "Indeed Application Closed" cells are real
        # checkbox cells (the append wrote a literal FALSE which Sheets
        # would render as text without explicit data validation).
        end_row = 1 + len(indeed_rows)
        try:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{
                    "setDataValidation": {
                        "range": {
                            "sheetId": inner_id,
                            "startRowIndex": 1,
                            "endRowIndex": end_row,
                            "startColumnIndex": 5,
                            "endColumnIndex": 6,
                        },
                        "rule": {
                            "condition": {"type": "BOOLEAN"},
                        },
                    }
                }]},
            ).execute()
        except Exception as e:
            log.warning("Could not apply checkbox validation: %s", e)

    log.info("Indeed Queue rebuilt: %d clean rows (incl. fresh checkboxes).",
             len(indeed_rows))


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


_EMAIL_PROVIDERS = (
    "gmail", "yahoo", "hotmail", "outlook", "aol", "icloud", "live",
    "msn", "protonmail", "ymail", "comcast", "verizon", "att",
    "sbcglobal", "cox", "charter", "earthlink", "mac", "qq",
)
_INDEED_ALIAS_TLDS = ("com", "net", "org", "co", "edu", "us")
_INDEED_ALIAS_RE = _re.compile(
    r"^(?P<local>.+?)(?P<prov>" + "|".join(_EMAIL_PROVIDERS) + r")"
    r"(?P<tld>" + "|".join(_INDEED_ALIAS_TLDS) + r")"
    r"(?:[_0-9a-z-]+)?@indeedemail\.com$",
    _re.IGNORECASE,
)


def _decode_indeed_alias(addr):
    """Indeed encodes the candidate's real email in the local part of
    its @indeedemail.com relay aliases:
        whitedaytona1988yahoocom9_nqm@indeedemail.com
        -> whitedaytona1988@yahoo.com
    Returns the decoded address or empty string on no match."""
    if not addr or "@indeedemail.com" not in addr.lower():
        return ""
    m = _INDEED_ALIAS_RE.match(addr.strip().lower())
    if not m:
        return ""
    local = m.group("local").rstrip("._-")
    if not local:
        return ""
    return local + "@" + m.group("prov") + "." + m.group("tld")


def _extract_email_from_drive(drive_svc, drive_cell: str, anthropic_api_key: str = "") -> str:
    """Pull the candidate email out of the resume PDF stored on Drive.

    Try cheap regex first (no API cost). If regex finds nothing AND we
    have an Anthropic key, send the PDF to Claude Haiku and ask it for
    the candidate's email. Haiku reading a PDF natively catches emails
    the regex misses (OCR-degraded text, unusual spacing, "name AT domain
    DOT com" obfuscation, etc.). Returns "" on any failure."""
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

    # Pass 1: cheap parse + regex
    text = ""
    try:
        from . import resume_parser
        text, _ = resume_parser.extract("resume", mime, data)
    except Exception as e:
        log.warning("resume_parser.extract failed for %s: %s", file_id, e)

    if text:
        for match in _EMAIL_REGEX.findall(text):
            addr = match.strip().rstrip(".,;:")
            decoded = _decode_indeed_alias(addr)
            if decoded:
                return decoded
            if not _is_job_board_email(addr):
                return addr

    # Pass 2: ask Claude with the PDF if available
    if anthropic_api_key and mime == "application/pdf" and data:
        try:
            email_from_llm = _ask_claude_for_email(anthropic_api_key, data)
            if email_from_llm and not _is_job_board_email(email_from_llm):
                return email_from_llm
        except Exception as e:
            log.warning("Claude email extract failed for %s: %s", file_id, e)

    return ""


def _ask_claude_for_email(api_key: str, pdf_bytes: bytes) -> str:
    """Send the PDF directly to Claude Haiku and ask for the candidate's
    email address. Returns empty string on no-match or any error.

    Claude reads the PDF natively (vision/document mode), so it picks
    up emails the regex misses -- e.g. when OCR mangled the @ sign, or
    the resume writes "name@domain.com" inside a header bar that the
    text extractor stripped."""
    import base64 as _b64
    import json as _json
    import anthropic
    encoded = _b64.standard_b64encode(pdf_bytes).decode("ascii")
    system = (
        "You read a single resume PDF and extract the candidate's real "
        "email address. Return ONLY a JSON object: {\"email\": \"<addr>\"} "
        "or {\"email\": \"\"} if no candidate email is visible. "
        "Do NOT return employer emails, generic recruiting addresses "
        "(jobs@, hr@, info@), or job-board relay aliases (anything "
        "@indeed.com, @indeedemail.com, @ziprecruiter.com, @glassdoor.com, "
        "@monster.com, @careerbuilder.com, @snagajob.com). Return ONLY "
        "the candidate's real email. CRITICAL: if the resume header shows an @indeedemail.com address like 'johnsmithgmailcom_xyz@indeedemail.com', that IS the candidate's email -- Indeed encoded it. Decode pattern: <localpart><provider><tld><suffix>@indeedemail.com where @ and . were stripped. Example: whitedaytona1988yahoocom9_nqm@indeedemail.com -> whitedaytona1988@yahoo.com. Common providers: gmail, yahoo, hotmail, outlook, aol, icloud. Common TLDs: com, net, org. Return the DECODED address, never the @indeedemail.com one. No prose, no code fences."
    )
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=80,
        system=system,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document",
                 "source": {"type": "base64",
                            "media_type": "application/pdf",
                            "data": encoded}},
                {"type": "text",
                 "text": "What is the candidate's real email address? Return JSON only."},
            ],
        }],
    )
    text = "".join(b.text for b in resp.content
                   if getattr(b, "type", "") == "text").strip()
    if text.startswith("```"):
        text = _re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = _re.sub(r"\n?```\s*$", "", text).strip()
    try:
        data = _json.loads(text)
    except Exception:
        return ""
    addr = str(data.get("email", "") or "").strip().rstrip(".,;:")
    return addr


def backfill_emails(svc, sheet_id, drive_svc, anthropic_key="",
                    tab=CANDIDATES_TAB, max_rows: int = 1000):
    """Walk Candidates, find rows with blank col C (Email), try to
    extract the candidate's email from the resume PDF on Drive (col O).
    Writes back to col C only if a non-job-board email was found.

    Reports per-row diagnostics so we can see why rows are still empty
    after the backfill (no Drive link / parse failed / no email in PDF)."""
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{tab}!A2:R",
        valueRenderOption="FORMULA",
    ).execute()
    rows = resp.get("values", []) or []
    log.info("Email backfill: %d Candidates rows", len(rows))

    # Diagnostic counters
    total_rows = len(rows)
    already_filled = 0
    no_drive_link = 0
    drive_parse_failed = 0
    no_email_in_pdf = 0
    attempted = 0
    found = 0
    updates = []
    sample_empty_no_drive = []   # row numbers of empty + no-Drive cases
    sample_no_email = []         # row numbers where Drive worked but no email

    for i, r in enumerate(rows, start=2):
        r = (r + [""] * 18)[:18]
        email = str(r[2]).strip()
        if email:
            already_filled += 1
            continue
        if attempted >= max_rows:
            log.info("Hit max_rows cap (%d); stopping early.", max_rows)
            break
        attempted += 1
        drive_link = str(r[14]).strip()  # col O = Drive File Link
        if not drive_link:
            no_drive_link += 1
            if len(sample_empty_no_drive) < 5:
                sample_empty_no_drive.append(i)
            updates.append({"range": f"{tab}!C{i}",
                            "values": [["Email not included"]]})
            continue
        extracted = _extract_email_from_drive(drive_svc, drive_link, anthropic_api_key=anthropic_key)
        if extracted:
            found += 1
            updates.append({"range": f"{tab}!C{i}",
                            "values": [[extracted]]})
            log.info("Row %d: extracted %s", i, extracted)
        else:
            # _extract_email_from_drive returns "" for any of: bad URL
            # parse, Drive download failure, parser empty, no email in
            # the extracted text. Treat as "no email" for the report.
            no_email_in_pdf += 1
            if len(sample_no_email) < 5:
                sample_no_email.append(i)
            updates.append({"range": f"{tab}!C{i}",
                            "values": [["Email not included"]]})

    if updates:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"data": updates, "valueInputOption": "USER_ENTERED"},
        ).execute()

    log.info("--- Email backfill report ---")
    log.info("  total rows:          %d", total_rows)
    log.info("  already had email:   %d", already_filled)
    log.info("  rows attempted:      %d", attempted)
    log.info("  found + wrote:       %d", found)
    log.info("  blank, no Drive:     %d  sample rows: %s",
             no_drive_link, sample_empty_no_drive)
    log.info("  blank, no email in PDF: %d  sample rows: %s",
             no_email_in_pdf, sample_no_email)


def hide_terminal_rows(svc, sheet_id, tab=CANDIDATES_TAB):
    """One-time backfill: hide every Candidates row where HR Status
    (col Q = col 17) is a terminal value. This was previously done by
    an onEdit Apps Script trigger; running it once in Python brings the
    sheet to the same end state without depending on the bound script."""
    inner_id = _get_sheet_id(svc, sheet_id, tab)
    if inner_id is None:
        log.warning("Could not find sheet id for %s; skipping hide", tab)
        return

    # Read column Q (HR Status) for all data rows.
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{tab}!Q2:Q",
    ).execute()
    vals = resp.get("values", []) or []
    terminal = {"Hired", "Rejected", "Not a fit", "Closed", "Withdrawn",
                "Declined", "Move forward", "Interviewing", "Offer Extended"}

    # Build a list of contiguous row ranges where status is terminal.
    rows_to_hide = []
    for i, v in enumerate(vals, start=2):
        cell = (v[0] if v else "") or ""
        if str(cell).strip() in terminal:
            rows_to_hide.append(i)
    if not rows_to_hide:
        log.info("hide_terminal_rows: nothing to hide.")
        return

    # Merge into contiguous ranges to minimize requests.
    requests = []
    start = rows_to_hide[0]
    prev = start
    for r in rows_to_hide[1:]:
        if r == prev + 1:
            prev = r
            continue
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": inner_id, "dimension": "ROWS",
                      "startIndex": start - 1, "endIndex": prev},
            "properties": {"hiddenByUser": True},
            "fields": "hiddenByUser",
        }})
        start = r
        prev = r
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": inner_id, "dimension": "ROWS",
                  "startIndex": start - 1, "endIndex": prev},
        "properties": {"hiddenByUser": True},
        "fields": "hiddenByUser",
    }})

    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests},
    ).execute()
    log.info("hide_terminal_rows: hid %d rows in %d range(s).",
             len(rows_to_hide), len(requests))


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


def fix_pending_query(svc, sheet_id, tab="Pending"):
    """Add/repair the Pending tab auto-populating QUERY.

    Pending tab headers (11 cols):
      A Days Pending | B Applied | C Name | D Email | E Phone |
      F Role | G Score | H AI Reasoning | I Drive | J HR Status |
      K HR Notes

    A: =ARRAYFORMULA(...) computing days since Applied timestamp.
    B: =QUERY(Candidates...) pulling pending_paused rows.

    Idempotent."""
    try:
        meta = svc.spreadsheets().get(
            spreadsheetId=sheet_id, fields="sheets.properties.title",
        ).execute()
    except Exception as e:
        log.warning("Could not load metadata: %s", e)
        return
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])
              if s.get("properties")]
    if tab not in titles:
        log.info("No %s tab; skipping.", tab)
        return

    # B2: QUERY pulls Applied (Timestamp), Name, Email, Phone, Role
    # (Applied For), Score (Confidence), AI Reasoning, Drive, HR Status,
    # HR Notes from Candidates where Decision=pending_paused.
    query_b = (
        '=IFERROR(QUERY(Candidates!A2:R, '
        '"SELECT A, B, C, D, G, M, N, O, Q, R '
        'WHERE J = \'pending_paused\' '
        'AND (Q IS NULL OR Q = \'\') '
        'ORDER BY A ASC", 0), "")'
    )
    # A2: Days Pending = today - applied date. ARRAYFORMULA spills
    # alongside the QUERY.
    formula_a = (
        '=ARRAYFORMULA(IF(B2:B="", "", '
        'IFERROR(DATEDIF(DATEVALUE(LEFT(B2:B,10)), TODAY(), "D"), "")))'
    )

    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{tab}!A2",
        valueInputOption="USER_ENTERED",
        body={"values": [[formula_a]]},
    ).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{tab}!B2",
        valueInputOption="USER_ENTERED",
        body={"values": [[query_b]]},
    ).execute()
    log.info("%s!A2 (ARRAYFORMULA days) + B2 (QUERY pending_paused) written.", tab)


def clean_indeed_queue_orphans(svc, sheet_id):
    """Indeed Queue cleanup. Removes two kinds of garbage:

    (a) Orphan rows where Timestamp is set but Name is empty -- these
        block backfill's dedupe-by-timestamp check from re-adding the
        candidate. We clear the timestamp so the backfill is free to
        re-add the row.

    (b) Duplicate empty-timestamp rows where Name is set but Timestamp
        is empty. These are the leftover OLD copies after a previous
        backfill produced new rows. We delete these rows entirely (use
        deleteDimension on the row range, batched in reverse order so
        indexes stay stable).

    Idempotent: subsequent runs find nothing to do."""
    inner_id = _get_sheet_id(svc, sheet_id, INDEED_QUEUE_TAB)
    if inner_id is None:
        log.warning("Could not find Indeed Queue sheet id")
        return
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{INDEED_QUEUE_TAB}!A2:G",
        ).execute()
    except Exception as e:
        log.warning("Could not read Indeed Queue: %s", e)
        return
    rows = resp.get("values", []) or []

    cleared = []                 # value updates: clear G on (no-name, ts)
    rows_to_delete = []          # row numbers to delete (no-ts, has-name)
    for i, r in enumerate(rows, start=2):
        r = (r + [""] * 7)[:7]
        name = str(r[0]).strip()
        ts = str(r[6]).strip()
        if (not name) and ts:
            cleared.append({"range": f"{INDEED_QUEUE_TAB}!G{i}",
                            "values": [[""]]})
        elif name and (not ts):
            rows_to_delete.append(i)

    if cleared:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"data": cleared, "valueInputOption": "USER_ENTERED"},
        ).execute()
        log.info("Cleared timestamp on %d (no-name, ts) rows.",
                 len(cleared))

    if rows_to_delete:
        # Delete in DESCENDING order so prior indices stay valid.
        delete_requests = []
        for row_num in sorted(rows_to_delete, reverse=True):
            delete_requests.append({
                "deleteDimension": {
                    "range": {
                        "sheetId": inner_id,
                        "dimension": "ROWS",
                        "startIndex": row_num - 1,
                        "endIndex": row_num,
                    }
                }
            })
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": delete_requests},
        ).execute()
        log.info("Deleted %d empty-timestamp duplicate rows: %s",
                 len(rows_to_delete), rows_to_delete)
    if not cleared and not rows_to_delete:
        log.info("Indeed Queue has no orphans or dup-empty-ts rows.")


def backfill_indeed_queue_columns(svc, sheet_id):
    """Fill in missing B (Position), C (Fit Quality), D (AI Recommendation)
    on existing Indeed Queue rows by looking up the matching Candidates
    row via the timestamp in col G.

    Rows that pre-existed our recent migration ended up with only
    Candidate Name + HR Status + Closed checkbox + Timestamp populated.
    This step walks every Indeed Queue row, and if B/C/D are blank,
    pulls Applied For (Candidates!G) + a derived Fit Quality + AI
    Recommendation from Candidates!J (Decision)."""
    # Read full Indeed Queue
    try:
        q_resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{INDEED_QUEUE_TAB}!A2:G",
        ).execute()
    except Exception as e:
        log.warning("Could not read Indeed Queue: %s", e)
        return
    q_rows = q_resp.get("values", []) or []
    if not q_rows:
        log.info("Indeed Queue empty -- nothing to backfill columns for.")
        return

    # Read Candidates A:R into a timestamp-indexed dict.
    try:
        c_resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{CANDIDATES_TAB}!A2:R",
        ).execute()
    except Exception as e:
        log.warning("Could not read Candidates for IQ backfill: %s", e)
        return
    c_rows = c_resp.get("values", []) or []
    by_ts = {}
    for r in c_rows:
        r = (r + [""] * 18)[:18]
        ts = str(r[0]).strip()
        if ts:
            by_ts[ts] = {
                "name": str(r[1]).strip(),
                "applied_for": str(r[6]).strip(),
                "decision": str(r[9]).strip(),
            }
    log.info("Loaded %d Candidates rows by timestamp for IQ join.",
             len(by_ts))

    updates = []
    filled_b = 0
    filled_c = 0
    filled_d = 0
    no_match = 0
    for i, r in enumerate(q_rows, start=2):
        r = (r + [""] * 7)[:7]
        ts = str(r[6]).strip()
        if not ts:
            continue
        cand = by_ts.get(ts)
        if not cand:
            no_match += 1
            continue

        # Position (col B)
        if not str(r[1]).strip() and cand["applied_for"]:
            updates.append({"range": f"{INDEED_QUEUE_TAB}!B{i}",
                            "values": [[cand["applied_for"]]]})
            filled_b += 1
        # Fit Quality (col C) -- derived from decision
        if not str(r[2]).strip() and cand["decision"]:
            fit = _FIT_QUALITY.get(cand["decision"], cand["decision"])
            updates.append({"range": f"{INDEED_QUEUE_TAB}!C{i}",
                            "values": [[fit]]})
            filled_c += 1
        # AI Recommendation (col D) -- derived from decision
        if not str(r[3]).strip() and cand["decision"]:
            rec = _AI_RECOMMENDATION.get(cand["decision"], "Review manually")
            updates.append({"range": f"{INDEED_QUEUE_TAB}!D{i}",
                            "values": [[rec]]})
            filled_d += 1

    if updates:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"data": updates, "valueInputOption": "USER_ENTERED"},
        ).execute()
    log.info("Indeed Queue columns backfilled: B=%d C=%d D=%d  no-match-in-Candidates=%d",
             filled_b, filled_c, filled_d, no_match)


def audit_all_tabs(svc, sheet_id):
    """Walk every tab in the spreadsheet and scan all formulas for
    references to Candidates columns. Identifies and auto-fixes ones
    that point at the OLD (pre-restructure) layout:

      A:P, 16              -> A:Q, 17     (HR Status moved)
      A:R range            -> A:R         (no change; new layout ends at R)
      Candidates!E:E       -> Candidates!F:F (old Filename was E; now F)
      Candidates!F:F       -> Candidates!G:G (old Applied For)
      ... etc. for every old col that shifted right by 1

    Skips: the Candidates tab itself (its own header row), any tab that
    has no formulas, Apps Script-bound triggers (those are server-side).

    Reports all formula touches and any patterns it couldn't auto-fix.
    """
    try:
        meta = svc.spreadsheets().get(
            spreadsheetId=sheet_id,
            fields="sheets.properties",
        ).execute()
    except Exception as e:
        log.warning("Could not load sheet metadata for audit: %s", e)
        return

    # The shift map for columns that moved right by 1 after the insert at E.
    # Old letter -> new letter. (Letters of cols that don't shift, like A-D,
    # are not in the map.) Old R/S/T were deleted, so any reference to them
    # is FATAL -- we report it but cannot auto-fix.
    shift_map = {
        "E": "F",  # Filename
        "F": "G",  # Applied For
        "G": "H",  # Cross-Fit Match
        "H": "I",  # Cross-Fit Flag
        "I": "J",  # Decision
        "J": "K",  # Years Exp
        "K": "L",  # Job Hopping
        "L": "M",  # Confidence
        "M": "N",  # AI Reasoning
        "N": "O",  # Drive Link
        "O": "P",  # Gmail Link
        "P": "Q",  # HR Status
        "Q": "R",  # HR Notes
    }
    deleted_letters = {"R", "S", "T", "U"}  # old Recruiter/Indeed/IndeedActionDone/AppSub

    fix_summary = {
        "tabs_scanned": 0,
        "formulas_seen": 0,
        "patched": 0,
        "patched_examples": [],
        "fatal_refs": [],  # references to deleted cols R/S/T/U
    }

    for s in meta.get("sheets", []):
        props = s.get("properties") or {}
        title = props.get("title", "")
        if not title or title in ("Candidates", "Inbox Log",
                                  "Archive - Misc", "Bot Errors",
                                  "Bot Learning Log", "Templates"):
            # Tabs the bot owns end-to-end -- their own writes are correct,
            # no user formulas to worry about.
            continue
        fix_summary["tabs_scanned"] += 1
        log.info("--- Auditing tab: %s ---", title)

        try:
            resp = svc.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=f"\'{title}\'!A1:Z1000",  # scan first 1000 rows, 26 cols
                valueRenderOption="FORMULA",
            ).execute()
        except Exception as e:
            log.warning("Could not read tab %s: %s", title, e)
            continue

        rows = resp.get("values", []) or []
        updates = []
        for ri, row in enumerate(rows, start=1):
            for ci, cell in enumerate(row):
                cv = str(cell or "")
                if not cv.startswith("="):
                    continue
                fix_summary["formulas_seen"] += 1
                if "Candidates!" not in cv and "Candidates'!" not in cv:
                    continue
                # Look for fatal refs to deleted cols
                for dl in deleted_letters:
                    pat = _re.search(
                        rf"Candidates(?:\'!|!)({dl}\d*:?{dl}?\d*|{dl}\d*\b)", cv)
                    if pat:
                        fix_summary["fatal_refs"].append(
                            f"{title}!{_col_letter(ci+1)}{ri}: {cv[:120]}")
                        break
                # Apply known fixes
                fixed = cv
                # 1. A:P, 16 -> A:Q, 17 (HR Status VLOOKUP idiom)
                fixed = _re.sub(
                    r"Candidates(\'?)!A:P,\s*16",
                    r"Candidates\1!A:Q,17",
                    fixed)
                # 2. Single-letter col references in Candidates!X:X or
                #    Candidates!X## form -- ONLY for columns that exist
                #    in the new layout (skip deleted letters).
                def shift_ref(match):
                    prefix = match.group(1)
                    letter = match.group(2)
                    rest = match.group(3) or ""
                    if letter in shift_map:
                        return f"Candidates{prefix}!{shift_map[letter]}{rest}"
                    return match.group(0)
                fixed = _re.sub(
                    r"Candidates(\'?)!([A-Z])(\d*|:[A-Z]\d*)",
                    shift_ref,
                    fixed)
                if fixed != cv:
                    cell_addr = f"{_col_letter(ci+1)}{ri}"
                    updates.append({
                        "range": f"\'{title}\'!{cell_addr}",
                        "values": [[fixed]]
                    })
                    fix_summary["patched"] += 1
                    if len(fix_summary["patched_examples"]) < 10:
                        fix_summary["patched_examples"].append(
                            f"{title}!{cell_addr}: {cv[:80]} -> {fixed[:80]}")

        if updates:
            try:
                svc.spreadsheets().values().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"data": updates,
                          "valueInputOption": "USER_ENTERED"},
                ).execute()
                log.info("Patched %d formula(s) on tab %s", len(updates), title)
            except Exception as e:
                log.warning("Could not write patches on %s: %s", title, e)

    log.info("--- AUDIT SUMMARY ---")
    log.info("Tabs scanned: %d", fix_summary["tabs_scanned"])
    log.info("Formulas seen: %d", fix_summary["formulas_seen"])
    log.info("Formulas patched: %d", fix_summary["patched"])
    for ex in fix_summary["patched_examples"]:
        log.info("  patched: %s", ex)
    if fix_summary["fatal_refs"]:
        log.warning("FATAL refs to deleted cols R/S/T/U (need manual review):")
        for ref in fix_summary["fatal_refs"][:20]:
            log.warning("  %s", ref)
        if len(fix_summary["fatal_refs"]) > 20:
            log.warning("  ... and %d more", len(fix_summary["fatal_refs"]) - 20)
    else:
        log.info("No fatal refs to deleted cols found.")


def dump_all_tabs_state(svc, sheet_id):
    """Walk every tab and log its header row + first data row + total
    row count + formula count. Also write the same report to a
    "Migration Report" tab on the spreadsheet so the user can read it
    directly without needing to dig into GitHub Actions logs."""
    try:
        meta = svc.spreadsheets().get(
            spreadsheetId=sheet_id,
            fields="sheets.properties",
        ).execute()
    except Exception as e:
        log.warning("Could not load sheet metadata: %s", e)
        return

    log.info("======== TAB STATE DUMP ========")

    # First: count true data rows on Candidates including hidden ones.
    # The Sheets API returns all rows regardless of hiddenByUser, so we
    # get the authoritative count here. This catches cases where the
    # hide_terminal_rows step has hidden many rows that gviz/QUERY
    # would otherwise exclude.
    try:
        c_resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{CANDIDATES_TAB}!A2:R",
        ).execute()
        c_rows = c_resp.get("values", []) or []
        non_empty = sum(1 for r in c_rows if r and str(r[0]).strip())
        log.info("Candidates true data rows (incl. hidden): %d", non_empty)

        # Count how many are hidden by checking sheet metadata
        meta_resp = svc.spreadsheets().get(
            spreadsheetId=sheet_id,
            ranges=[f"{CANDIDATES_TAB}!A:A"],
            fields="sheets(properties,data.rowMetadata)",
        ).execute()
        hidden_count = 0
        terminal_count = 0
        from collections import Counter as _Counter
        appsub_counter = _Counter()
        hr_status_counter = _Counter()
        for r in c_rows:
            if not r or not str(r[0]).strip():
                continue
            appsub = (r[4] if len(r) > 4 else "") or ""
            hr_status = (r[16] if len(r) > 16 else "") or ""
            appsub_counter[appsub] += 1
            hr_status_counter[hr_status] += 1
            if hr_status.strip() in {"Hired","Rejected","Not a fit","Closed",
                                     "Withdrawn","Declined","Move forward",
                                     "Interviewing","Offer Extended"}:
                terminal_count += 1
        log.info("Application Submitted breakdown: %s",
                 dict(appsub_counter.most_common()))
        log.info("HR Status breakdown: %s",
                 dict(hr_status_counter.most_common()))
        log.info("Rows with terminal HR Status (hidden by Apps Script): %d",
                 terminal_count)

        # Read hiddenByUser flag for each row from sheet metadata
        for s in meta_resp.get("sheets", []):
            props = s.get("properties") or {}
            if props.get("title") != CANDIDATES_TAB:
                continue
            for data_block in s.get("data") or []:
                meta_rows = data_block.get("rowMetadata") or []
                for i, mr in enumerate(meta_rows):
                    if mr.get("hiddenByUser"):
                        hidden_count += 1
            break
        log.info("Rows hidden by user (hiddenByUser=true): %d", hidden_count)
    except Exception as e:
        log.warning("Could not count true Candidates rows: %s", e)

    for s in meta.get("sheets", []):
        props = s.get("properties") or {}
        title = props.get("title", "")
        grid = props.get("gridProperties") or {}
        row_count = grid.get("rowCount", 0)
        col_count = grid.get("columnCount", 0)

        log.info("--- Tab: %s  (grid: %d rows x %d cols) ---",
                 title, row_count, col_count)

        # Header row + first data row, values
        try:
            v_resp = svc.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=f"\'{title}\'!A1:Z2",
            ).execute()
            v_rows = v_resp.get("values", []) or []
            header = v_rows[0] if v_rows else []
            data1  = v_rows[1] if len(v_rows) > 1 else []
            log.info("  Header row (%d cols): %s", len(header), header)
            log.info("  First data row    : %s",
                     [str(c)[:60] for c in data1])
        except Exception as e:
            log.warning("  Could not read values: %s", e)
            continue

        # Formula scan on first 100 rows
        try:
            f_resp = svc.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=f"\'{title}\'!A1:Z100",
                valueRenderOption="FORMULA",
            ).execute()
            f_rows = f_resp.get("values", []) or []
            formulas = []
            for ri, row in enumerate(f_rows, start=1):
                for ci, cell in enumerate(row):
                    cv = str(cell or "")
                    if cv.startswith("="):
                        formulas.append((ri, ci, cv))
            log.info("  Formulas in first 100 rows: %d", len(formulas))
            # Log first 5 unique formula patterns
            seen = set()
            for ri, ci, cv in formulas:
                pattern = cv[:120]
                if pattern in seen:
                    continue
                seen.add(pattern)
                log.info("    sample: %s%d -> %s",
                         _col_letter(ci+1), ri, pattern)
                if len(seen) >= 5:
                    break
            # Flag stale references
            for ri, ci, cv in formulas:
                low = cv.lower()
                # References to old HR Status position
                if "candidates!a:p" in low and ",16" in low.replace(" ", ""):
                    log.warning("    STALE A:P,16 reference at %s%d: %s",
                                _col_letter(ci+1), ri, cv[:120])
                # References to deleted cols (anything Candidates!R/S/T
                # past row 1, since those cols are gone -- exclude pattern
                # like "Candidates!R" used as "Candidates!R{row}" though
                # that's also a deleted ref now).
                m = _re.search(r"Candidates(?:\'?)!([RST])(\d|:|$)", cv)
                if m and not _re.search(r"Candidates(?:\'?)!R(?![A-Z])\b", cv):
                    log.warning("    REF TO DELETED COL at %s%d: %s",
                                _col_letter(ci+1), ri, cv[:120])
        except Exception as e:
            log.warning("  Could not read formulas: %s", e)

    log.info("======== END TAB STATE DUMP ========")

    # Also write the report to a "Migration Report" tab so the user can
    # see the same information without digging into Actions logs.
    report_tab = "Migration Report"
    if _get_sheet_id(svc, sheet_id, report_tab) is None:
        try:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": report_tab}}}]},
            ).execute()
            log.info("Created %s tab.", report_tab)
        except Exception as e:
            log.warning("Could not create %s tab: %s", report_tab, e)
            return

    # Re-collect the same info but as rows for the report tab.
    report_rows = [[
        "Tab", "Grid Rows", "Grid Cols", "Header Count",
        "First Data Row Preview", "Formulas In First 100",
        "Notes", "Audited At",
    ]]
    from datetime import datetime as _dt, timezone as _tz
    audited = _dt.now(_tz.utc).isoformat(timespec="seconds")
    try:
        meta2 = svc.spreadsheets().get(
            spreadsheetId=sheet_id, fields="sheets.properties",
        ).execute()
    except Exception:
        meta2 = {"sheets": []}
    for s2 in meta2.get("sheets", []):
        p = s2.get("properties") or {}
        title = p.get("title", "")
        if title == report_tab:
            continue
        grid = p.get("gridProperties") or {}
        r_count = grid.get("rowCount", 0)
        c_count = grid.get("columnCount", 0)
        try:
            v = svc.spreadsheets().values().get(
                spreadsheetId=sheet_id, range=f"\'{title}\'!A1:Z2",
            ).execute()
            rows2 = v.get("values", []) or []
            hdr = rows2[0] if rows2 else []
            d1 = rows2[1] if len(rows2) > 1 else []
        except Exception:
            hdr, d1 = [], []
        try:
            f = svc.spreadsheets().values().get(
                spreadsheetId=sheet_id, range=f"\'{title}\'!A1:Z100",
                valueRenderOption="FORMULA",
            ).execute()
            f_rows = f.get("values", []) or []
            fcount = sum(1 for row in f_rows
                         for cell in row
                         if str(cell or "").startswith("="))
        except Exception:
            fcount = 0
        # Notes: flag obvious issues
        notes = []
        if not hdr:
            notes.append("EMPTY HEADER")
        if len(hdr) < 2:
            notes.append("<2 cols")
        # Check for stale refs in any cell formula
        if title not in ("Candidates", "Inbox Log", "Archive - Misc",
                         "Bot Errors", "Bot Learning Log"):
            for row in f_rows:
                for cell in row:
                    cv = str(cell or "")
                    if "Candidates!A:P" in cv and ",16" in cv.replace(" ", ""):
                        notes.append("stale A:P,16 ref")
                        break
                if notes and notes[-1] == "stale A:P,16 ref":
                    break

        report_rows.append([
            title,
            str(r_count),
            str(c_count),
            str(len(hdr)),
            " | ".join(str(c)[:40] for c in d1[:8]),
            str(fcount),
            "; ".join(notes) if notes else "ok",
            audited,
        ])

    # Compute Candidates-specific deep stats for the report
    candidates_stats_rows = [
        ["", "", "", "", "", "", "", ""],
        ["CANDIDATES DEEP STATS", "", "", "", "", "", "", ""],
    ]
    try:
        c_resp2 = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"{CANDIDATES_TAB}!A2:R",
        ).execute()
        c_rows2 = c_resp2.get("values", []) or []
        non_empty2 = [r for r in c_rows2 if r and str(r[0]).strip()]
        candidates_stats_rows.append(["Total data rows (incl. hidden)", str(len(non_empty2)),
                                       "", "", "", "", "", ""])

        from collections import Counter as _C
        appsub_c = _C()
        hr_c = _C()
        terminal_n = 0
        terminal_set = {"Hired","Rejected","Not a fit","Closed","Withdrawn",
                        "Declined","Move forward","Interviewing","Offer Extended"}
        for r in non_empty2:
            r = (r + [""]*18)[:18]
            appsub_c[r[4] or "(blank)"] += 1
            hr_c[r[16] or "(blank)"] += 1
            if (r[16] or "").strip() in terminal_set:
                terminal_n += 1

        candidates_stats_rows.append(["Terminal HR Status (should be hidden)",
                                       str(terminal_n), "", "", "", "", "", ""])
        candidates_stats_rows.append(["", "", "", "", "", "", "", ""])
        candidates_stats_rows.append(["Application Submitted breakdown:",
                                       "", "", "", "", "", "", ""])
        for k, v in appsub_c.most_common():
            candidates_stats_rows.append(["  " + str(k), str(v),
                                          "", "", "", "", "", ""])
        candidates_stats_rows.append(["", "", "", "", "", "", "", ""])
        candidates_stats_rows.append(["HR Status breakdown:",
                                       "", "", "", "", "", "", ""])
        for k, v in hr_c.most_common():
            candidates_stats_rows.append(["  " + str(k), str(v),
                                          "", "", "", "", "", ""])

        # Count rows actually hidden via metadata
        try:
            meta_resp2 = svc.spreadsheets().get(
                spreadsheetId=sheet_id,
                ranges=[f"{CANDIDATES_TAB}!A:A"],
                fields="sheets(properties.title,data.rowMetadata.hiddenByUser)",
            ).execute()
            hidden_total = 0
            for sh in meta_resp2.get("sheets", []):
                if (sh.get("properties") or {}).get("title") != CANDIDATES_TAB:
                    continue
                for blk in sh.get("data") or []:
                    for rm in blk.get("rowMetadata") or []:
                        if rm.get("hiddenByUser"):
                            hidden_total += 1
            candidates_stats_rows.append(["", "", "", "", "", "", "", ""])
            candidates_stats_rows.append(["Rows hidden by user (metadata count)",
                                          str(hidden_total),
                                          "", "", "", "", "", ""])
        except Exception as e:
            candidates_stats_rows.append(["(hidden count failed)", str(e)[:60],
                                          "", "", "", "", "", ""])

    except Exception as e:
        candidates_stats_rows.append(["(stats failed)", str(e)[:80],
                                      "", "", "", "", "", ""])

    report_rows.extend(candidates_stats_rows)

    try:
        # Clear the tab then write the report
        svc.spreadsheets().values().clear(
            spreadsheetId=sheet_id, range=f"\'{report_tab}\'!A:Z",
        ).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"\'{report_tab}\'!A1",
            valueInputOption="RAW",
            body={"values": report_rows},
        ).execute()
        log.info("Wrote %d-row report to %s tab.", len(report_rows), report_tab)
    except Exception as e:
        log.warning("Could not write report tab: %s", e)


def run() -> int:
    cfg = config.load()
    log.info("Migration starting. Sheet=%s", cfg.sheet_id)
    creds = google_auth.make_credentials(
        cfg.oauth_client_id, cfg.oauth_client_secret, cfg.oauth_refresh_token
    )
    svc = google_auth.sheets(creds)

    log.info("STEP 1: Candidates tab restructure")
    migrate_candidates(svc, cfg.sheet_id)

    log.info("STEP 2: Indeed Queue formula update + Cross-Match QUERY fix + Pending QUERY fix + col backfill")
    fix_indeed_queue_formulas(svc, cfg.sheet_id)
    fix_cross_match_query(svc, cfg.sheet_id)
    fix_pending_query(svc, cfg.sheet_id)
    backfill_indeed_queue_columns(svc, cfg.sheet_id)

    log.info("STEP 3: Indeed Queue rebuild (Gmail enrichment then full wipe + repopulate + final orphan sweep)")
    gmail_svc = google_auth.gmail(creds)
    enrich_indeed_from_gmail(svc, cfg.sheet_id, gmail_svc, cfg.gmail_user)
    rebuild_indeed_queue(svc, cfg.sheet_id)
    clean_indeed_queue_orphans(svc, cfg.sheet_id)

    log.info("STEP 4: Gmail link backfill (authuser=jobs@)")
    backfill_gmail_links(svc, cfg.sheet_id)

    log.info("STEP 5: Email backfill (extract from resume PDFs on Drive, Claude fallback)")
    drive_svc = google_auth.drive(creds)
    backfill_emails(svc, cfg.sheet_id, drive_svc,
                    anthropic_key=cfg.anthropic_api_key)

    log.info("STEP 6: Diagnostic dump of rows 13 and 26 for review")
    inspect_row(svc, cfg.sheet_id, CANDIDATES_TAB, 13)
    inspect_row(svc, cfg.sheet_id, CANDIDATES_TAB, 26)

    log.info("STEP 7: Hide rows with terminal HR Status (replacement for Apps Script onEdit)")
    hide_terminal_rows(svc, cfg.sheet_id)

    log.info("STEP 8: Audit all other tabs + auto-fix formulas pointing at old layout")
    audit_all_tabs(svc, cfg.sheet_id)

    log.info("STEP 9: Final state dump of every tab")
    dump_all_tabs_state(svc, cfg.sheet_id)

    log.info("Migration complete.")
    return 0



def backfill_prior_rejection(svc, sheet_id, tab=CANDIDATES_TAB):
    """For every Candidates row, set col S to "🚩 Previously rejected"
    if an earlier row exists with the same trimmed/case-insensitive name
    AND a terminal-rejection HR Status (Rejected / Not Selected / Not a fit).
    Idempotent."""
    REJECTED = {"Rejected", "Not Selected", "Not a fit"}
    FLAG = "🚩 Previously rejected"
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{tab}!A2:R",
    ).execute()
    rows = resp.get("values", []) or []
    log.info("prior-rejection backfill: %d Candidates rows", len(rows))
    history = {}
    for i, r in enumerate(rows):
        r = (r + [""] * 18)[:18]
        name = str(r[1] or "").strip().lower()
        if not name:
            continue
        hr_status = str(r[16] or "").strip()
        history.setdefault(name, []).append((i, hr_status))
    updates = []
    flagged = 0
    for i, r in enumerate(rows):
        r = (r + [""] * 18)[:18]
        name = str(r[1] or "").strip().lower()
        flag = ""
        if name:
            for j, prev_status in history.get(name, []):
                if j < i and prev_status in REJECTED:
                    flag = FLAG
                    flagged += 1
                    break
        updates.append({"range": f"{tab}!S{i + 2}", "values": [[flag]]})
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"{tab}!S1",
        valueInputOption="RAW",
        body={"values": [["Prior Rejection"]]},
    ).execute()
    if updates:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"data": updates, "valueInputOption": "USER_ENTERED"},
        ).execute()
    log.info("prior-rejection backfill: flagged %d row(s) of %d", flagged, len(rows))


def run_prior_rejection_only() -> int:
    """Set col S header and backfill the duplicate-rejection flag. Idempotent."""
    cfg = config.load()
    log.info("Prior-rejection backfill. Sheet=%s", cfg.sheet_id)
    creds = google_auth.make_credentials(
        cfg.oauth_client_id, cfg.oauth_client_secret, cfg.oauth_refresh_token
    )
    svc = google_auth.sheets(creds)
    backfill_prior_rejection(svc, cfg.sheet_id)
    return 0


def run_email_only() -> int:
    """Backfill emails only -- skips the destructive Indeed Queue rebuild
    and other already-completed migration steps. Triggered when the
    workflow_dispatch input `steps` is 'emails' or 'email'.
    """
    cfg = config.load()
    log.info("Email-only backfill. Sheet=%s", cfg.sheet_id)
    creds = google_auth.make_credentials(
        cfg.oauth_client_id, cfg.oauth_client_secret, cfg.oauth_refresh_token
    )
    svc = google_auth.sheets(creds)
    drive_svc = google_auth.drive(creds)
    backfill_emails(svc, cfg.sheet_id, drive_svc, anthropic_key=cfg.anthropic_api_key)
    return 0


if __name__ == "__main__":
    import os as _os
    _steps = (_os.environ.get("MIGRATE_STEPS", "all") or "all").strip().lower()
    if _steps in ("emails", "email"):
        sys.exit(run_email_only())
    if _steps in ("prior_rejection", "rejection", "dup"):
        sys.exit(run_prior_rejection_only())
    sys.exit(run())
