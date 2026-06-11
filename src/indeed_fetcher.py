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

R1 Concepts uses Avanan (Check Point) email security, which rewrites
every URL in incoming mail to url.avanan.click/v2/... for malware
scanning. We detect the Avanan wrapper, follow the redirect to recover
the real Indeed URL, then proceed normally. The Avanan redirect may
land on cts.indeed.com (Indeed's click-tracking service) which is an
intermediate hop that requests doesn't auto-follow; we recover the real
URL or id token directly from the CTS query params in that case.

If Indeed ever changes this contract -- adds auth, expires tokens
sooner, moves the endpoint -- the fetch fails and main.py falls
through to the Needs Human queue. No data is lost, HR just clicks
through manually as a fallback.
"""

from __future__ import annotations

import base64
import logging
import re
from urllib.parse import urlparse, parse_qs, urlencode, unquote

import requests

log = logging.getLogger(__name__)


# Match the 'View resume' URL Indeed embeds in application emails:
#   https://employers.indeed.com/candidates/resume?refUid=...&id=<TOKEN>&ctx=...
_VIEW_URL_PATTERN = re.compile(
    r"https?://employers\.indeed\.com/candidates/resume\?[^\s\"'<>]+",
    re.IGNORECASE,
)

# Avanan/Check Point URL rewrite. R1 Concepts has this enabled, so the
# real Indeed URL is wrapped: https://url.avanan.click/v2/<segments>...
_AVANAN_URL_PATTERN = re.compile(
    r"https?://url\.avanan\.click/v2/[^\s\"'<>]+",
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
    found. Tries direct Indeed URL first, then Avanan-wrapped fallback."""
    if not email_body:
        return None
    match = _VIEW_URL_PATTERN.search(email_body)
    if match:
        return match.group(0)
    # Avanan-wrapped URL fallback. Find Avanan URL near 'View resume' text.
    lower_body = email_body.lower()
    anchor_idx = lower_body.find("view resume")
    if anchor_idx < 0:
        return None
    window_end = min(len(email_body), anchor_idx + 2000)
    window = email_body[anchor_idx:window_end]
    av_match = _AVANAN_URL_PATTERN.search(window)
    if av_match:
        return av_match.group(0)
    look_back_start = max(0, anchor_idx - 1500)
    back_window = email_body[look_back_start:anchor_idx]
    back_matches = _AVANAN_URL_PATTERN.findall(back_window)
    if back_matches:
        return back_matches[-1]
    return None


def _unwrap_avanan(url: str) -> str | None:
    """If url is an Avanan-wrapped URL, follow the redirect chain to the
    real destination. Returns the original URL if not Avanan-wrapped,
    or None on fetch failure. Handles the cts.indeed.com intermediate
    hop (Indeed click-tracking) which requests may not auto-follow."""
    if "url.avanan.click" not in url.lower():
        return url
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_FETCH_TIMEOUT_SECONDS,
            allow_redirects=True,
            stream=True,
        )
        final_url = resp.url
        resp.close()
    except requests.RequestException as e:
        log.warning("indeed_fetcher: avanan unwrap failed: %s", type(e).__name__)
        return None
    host = (urlparse(final_url or "").hostname or "").lower()
    if "employers.indeed.com" in host:
        return final_url
    if host.endswith("indeed.com"):
        # Indeed click-tracking subdomain (cts.indeed.com etc).
        unwrapped = _recover_indeed_url_from_cts(final_url)
        if unwrapped:
            return unwrapped
        log.warning(
            "indeed_fetcher: avanan -> %s but couldn't recover real URL", host,
        )
        return None
    log.warning(
        "indeed_fetcher: avanan redirect did not land on Indeed (host=%s)", host,
    )
    return None


def _recover_indeed_url_from_cts(cts_url: str) -> str | None:
    """Given a cts.indeed.com URL, recover the real employers.indeed.com
    URL. CTS typically embeds the destination URL as a query param
    ('p', 'rdr', 'redirectUrl', 'url', 'r', 'target', 'to'). The value
    may be URL-encoded once or twice, or base64-encoded.

    Falls back to extracting just the id token directly from the CTS
    query params if no embedded URL is found -- the download endpoint
    only needs the id, not the full original URL."""
    try:
        parsed = urlparse(cts_url)
        params = parse_qs(parsed.query)
    except Exception:
        return None
    candidate_keys = ("p", "rdr", "redirectUrl", "url", "r", "target", "to")
    for key in candidate_keys:
        for raw_val in params.get(key, []):
            for decoded in (raw_val, unquote(raw_val), unquote(unquote(raw_val))):
                if "employers.indeed.com" in decoded:
                    return decoded
            try:
                b64 = base64.urlsafe_b64decode(raw_val + "==").decode(
                    "utf-8", errors="ignore"
                )
                if "employers.indeed.com" in b64:
                    return b64.split()[0]
            except Exception:
                pass
    # Fallback: CTS URL may have the resume id token directly. Build a
    # synthetic employers URL containing it -- build_download_url only
    # needs the id.
    for raw_val in params.get("id", []):
        if raw_val:
            return f"https://employers.indeed.com/candidates/resume?id={raw_val}"
    return None


def build_download_url(view_resume_url: str) -> str | None:
    """Given the 'View resume' URL from the email, build the public PDF
    download URL. The view URL has ?id=<token>; the download endpoint
    takes the same token. Returns None if the URL lacks an id param.
    Transparently unwraps Avanan-wrapped URLs first."""
    real_url = _unwrap_avanan(view_resume_url)
    if not real_url:
        return None
    try:
        parsed = urlparse(real_url)
        params = parse_qs(parsed.query)
    except Exception:
        return None
    resume_id = (params.get("id") or [""])[0]
    if not resume_id:
        return None
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
