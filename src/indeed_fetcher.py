"""Fetch resumes from Indeed Quick Apply emails.

When a candidate applies via Indeed without uploading a PDF themselves,
the forwarded application email contains only a 'View resume' link to
Indeed's public resume viewer. This module follows that link's id token
to Indeed's public PDF download endpoint and returns the PDF bytes so
the bot can score the candidate the same way it would any other
applicant who attached a resume directly.

Indeed's download endpoint is a tokenized public URL designed for
forwarding (no auth required -- verified in incognito). The token in
the email's 'View resume' link is the same token used by the download
endpoint, so we extract it from the email and rebuild the download URL
ourselves rather than scraping the HTML viewer page.

If Indeed ever changes this contract -- adds auth, expires tokens
sooner, moves the endpoint -- the fetch fails and main.py falls
through to the Needs Human queue. No data is lost, HR just clicks
through manually as a fallback.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse, parse_qs, urlencode

import requests

log = logging.getLogger(__name__)


# Match the 'View resume' URL Indeed embeds in application emails:
#   https://employers.indeed.com/candidates/resume?refUid=...&id=<TOKEN>&ctx=...
_VIEW_URL_PATTERN = re.compile(
    r"https?://employers\.indeed\.com/candidates/resume\?[^\s\"'<>]+",
    re.IGNORECASE,
)

# Indeed's public PDF endpoint. Path includes /public/ which strongly
# signals Indeed designed this for un-auth'd sharing.
_DOWNLOAD_ENDPOINT = "https://employers.indeed.com/api/catws/public/resume/download"

# Look like a real browser. Indeed will see the GHA runner's IP either
# way; the UA mainly avoids the obvious python-requests/* signature.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Fail fast on weirdness so main.py can fall through to Needs Human
# rather than holding up the whole run on a slow Indeed response.
_FETCH_TIMEOUT_SECONDS = 15

# Sanity caps on the response so a misbehaving endpoint can't blow up
# memory / Drive uploads. Real resumes are well under this.
_MAX_PDF_BYTES = 15 * 1024 * 1024  # 15 MB


def extract_view_resume_url(email_body: str) -> str | None:
    """Find the 'View resume' link in an Indeed Quick Apply email body.
    Returns the first matching URL or None if no candidate link is
    found. Indeed-templated emails contain exactly one such link."""
    if not email_body:
        return None
    match = _VIEW_URL_PATTERN.search(email_body)
    return match.group(0) if match else None


def build_download_url(view_resume_url: str) -> str | None:
    """Given the 'View resume' URL from the email, build the public PDF
    download URL. The view URL has ?id=<token>; the download endpoint
    takes the same token. Returns None if the URL lacks an id param."""
    try:
        parsed = urlparse(view_resume_url)
        params = parse_qs(parsed.query)
    except Exception:
        return None
    resume_id = (params.get("id") or [""])[0]
    if not resume_id:
        return None
    # publicResumeTk is JS-populated client-side; in the public-link
    # context the literal string 'undefined' is what the browser sends,
    # and the endpoint accepts it.
    query = urlencode({"id": resume_id, "publicResumeTk": "undefined"})
    return f"{_DOWNLOAD_ENDPOINT}?{query}"


def fetch_resume_pdf(view_resume_url: str) -> bytes | None:
    """Fetch the resume PDF bytes for an Indeed Quick Apply candidate.

    Returns the PDF bytes on success, None on any failure. Failures are
    logged but never raised -- callers should treat None as 'fall
    through to manual handling'. Does not log PII (candidate name,
    email, etc.) -- just the URL pattern and HTTP details."""
    download_url = build_download_url(view_resume_url)
    if not download_url:
        log.warning("indeed_fetcher: could not build download URL from view URL")
        return None
    try:
        resp = requests.get(
            download_url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/pdf,*/*",
            },
            timeout=_FETCH_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        log.warning("indeed_fetcher: request failed: %s", type(e).__name__)
        return None
    if resp.status_code != 200:
        log.warning(
            "indeed_fetcher: HTTP %d from Indeed download endpoint",
            resp.status_code,
        )
        return None
    content_type = (resp.headers.get("content-type") or "").lower()
    if "pdf" not in content_type:
        # Could be an HTML error page, JSON error, redirect to login,
        # etc. -- not a real PDF, so don't try to score it.
        log.warning(
            "indeed_fetcher: unexpected content-type=%r (expected pdf)",
            content_type,
        )
        return None
    body = resp.content
    if not body:
        log.warning("indeed_fetcher: empty response body")
        return None
    if not body.startswith(b"%PDF"):
        # Defense-in-depth: even with a pdf content-type, verify the
        # magic bytes. An HTML interstitial pretending to be a PDF
        # would not match.
        log.warning(
            "indeed_fetcher: response body is not a PDF (first bytes=%r)",
            body[:8],
        )
        return None
    if len(body) > _MAX_PDF_BYTES:
        log.warning(
            "indeed_fetcher: PDF too large (%d bytes > %d limit)",
            len(body), _MAX_PDF_BYTES,
        )
        return None
    log.info("indeed_fetcher: fetched %d-byte PDF from Indeed", len(body))
    return body
