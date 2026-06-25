"""One-time inbox cleanup: archive Gmail threads on/before a target date.

Triggered manually via the 'Archive inbox before date' GitHub Actions
workflow. Reads the target date and dry-run flag from env vars, lists
every thread currently in jobs@ INBOX whose latest message is on or
before that date, optionally skips threads HR is actively working
(any For HR/* label), and removes the INBOX label.

Action is reversible: the threads are still in Gmail under All Mail
and any existing labels stay attached. Nothing is deleted.

Env vars:
  GOOGLE_OAUTH_CLIENT_ID       (required)
  GOOGLE_OAUTH_CLIENT_SECRET   (required)
  GOOGLE_OAUTH_REFRESH_TOKEN   (required)
  GMAIL_USER                   (required, e.g. jobs@r1concepts.com)
  ARCHIVE_BEFORE_DATE          (required, YYYY-MM-DD)
  DRY_RUN                      (optional, default 'true'; set to
                                'false' to actually archive)
  SKIP_FOR_HR                  (optional, default 'true'; skip threads
                                with any For HR/* label)
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time

from src import google_auth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("archive_before_date")


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _bool_env(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def _list_for_hr_label_ids(gmail, user: str) -> set[str]:
    """Return label IDs for every For HR / For HR/* label so we can
    detect threads HR is actively working."""
    try:
        resp = gmail.users().labels().list(userId=user).execute()
    except Exception as exc:
        log.warning("could not list labels: %s -- HR-skip filter disabled", exc)
        return set()
    ids = set()
    for lbl in resp.get("labels", []) or []:
        name = lbl.get("name", "")
        if name == "For HR" or name.startswith("For HR/"):
            ids.add(lbl["id"])
    return ids


def _iter_threads(gmail, user: str, query: str):
    """Yield thread dicts page-by-page for the given Gmail search query."""
    page_token = None
    while True:
        kwargs = {
            "userId": user,
            "q": query,
            "maxResults": 500,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        try:
            resp = gmail.users().threads().list(**kwargs).execute()
        except Exception as exc:
            log.error("threads().list failed: %s", exc)
            return
        threads = resp.get("threads", []) or []
        for t in threads:
            yield t
        page_token = resp.get("nextPageToken")
        if not page_token:
            return


def _thread_label_ids(gmail, user: str, thread_id: str) -> list[str]:
    """Return union of labelIds across all messages in a thread."""
    try:
        t = gmail.users().threads().get(
            userId=user, id=thread_id, format="minimal",
        ).execute()
    except Exception as exc:
        log.warning("thread().get(%s) failed: %s", thread_id, exc)
        return []
    ids: set[str] = set()
    for m in t.get("messages", []) or []:
        for lid in m.get("labelIds", []) or []:
            ids.add(lid)
    return list(ids)


def _archive_thread(gmail, user: str, thread_id: str) -> bool:
    """Remove INBOX label. Returns True on success."""
    try:
        gmail.users().threads().modify(
            userId=user, id=thread_id,
            body={"removeLabelIds": ["INBOX"]},
        ).execute()
        return True
    except Exception as exc:
        log.warning("archive thread %s failed: %s", thread_id, exc)
        return False


def main() -> int:
    target_date = (os.environ.get("ARCHIVE_BEFORE_DATE") or "").strip()
    if not _DATE_RE.match(target_date):
        log.error(
            "ARCHIVE_BEFORE_DATE must be YYYY-MM-DD; got %r",
            target_date,
        )
        return 2

    gmail_user = (os.environ.get("GMAIL_USER") or "").strip()
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or ""
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or ""
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN") or ""
    if not (gmail_user and client_id and client_secret and refresh_token):
        log.error("missing one of GMAIL_USER / GOOGLE_OAUTH_* env vars")
        return 2

    dry_run = _bool_env("DRY_RUN", True)
    skip_for_hr = _bool_env("SKIP_FOR_HR", True)

    # Gmail search uses YYYY/MM/DD. 'before:' is exclusive of the date,
    # so for an inclusive on-or-before we bump by one day below.
    # Easier: use 'before:<date+1day>' semantics by adding a day.
    from datetime import date, timedelta
    target = date.fromisoformat(target_date)
    gmail_before = (target + timedelta(days=1)).strftime("%Y/%m/%d")
    query = f"in:inbox before:{gmail_before}"

    mode = "DRY-RUN" if dry_run else "LIVE"
    log.info(
        "%s | user=%s | archiving threads in INBOX with latest message "
        "on or before %s | skip_for_hr=%s | query=%r",
        mode, gmail_user, target_date, skip_for_hr, query,
    )

    creds = google_auth.make_credentials(client_id, client_secret, refresh_token)
    gmail = google_auth.gmail(creds)

    for_hr_ids: set[str] = set()
    if skip_for_hr:
        for_hr_ids = _list_for_hr_label_ids(gmail, gmail_user)
        log.info("loaded %d For HR/* label IDs to skip", len(for_hr_ids))

    eligible = 0
    skipped_for_hr = 0
    archived = 0
    errors = 0
    sample_subjects: list[str] = []

    for t in _iter_threads(gmail, gmail_user, query):
        tid = t.get("id")
        if not tid:
            continue
        snippet = (t.get("snippet") or "")[:140]
        if skip_for_hr and for_hr_ids:
            label_ids = set(_thread_label_ids(gmail, gmail_user, tid))
            if label_ids & for_hr_ids:
                skipped_for_hr += 1
                continue
        eligible += 1
        if len(sample_subjects) < 20:
            sample_subjects.append(f"  - {tid}: {snippet}")
        if dry_run:
            continue
        if _archive_thread(gmail, gmail_user, tid):
            archived += 1
        else:
            errors += 1
        # Be polite to Gmail's quota; one modify per ~50ms is plenty.
        time.sleep(0.05)

    log.info("=" * 60)
    log.info("eligible threads        : %d", eligible)
    log.info("skipped (For HR/*)      : %d", skipped_for_hr)
    if dry_run:
        log.info("DRY RUN -- nothing was archived. Re-run with DRY_RUN=false")
        log.info("           to actually remove the INBOX label.")
    else:
        log.info("archived (INBOX off)    : %d", archived)
        log.info("errors                  : %d", errors)
    if sample_subjects:
        log.info("first %d eligible threads:", len(sample_subjects))
        for s in sample_subjects:
            log.info(s)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
