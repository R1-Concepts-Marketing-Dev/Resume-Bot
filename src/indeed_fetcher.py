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
every URL in incoming mail to url.avanan.click/v2/<random>/___<dest>___.<sig>
for malware scanning. The Avanan v2 format embeds the real destination
URL INLINE between '___' separators, so we extract it without any HTTP
fetch. The destination is typically a cts.indeed.com click-tracking URL
which itself encodes the final employers.indeed.com URL as a
gzip-base64-JSON blob in its path. We decode that blob locally too;
no HTTP redirect-following needed.

If Indeed ever changes this contract -- adds auth, expires tokens
sooner, moves the endpoint -- the fetch fails and main.py falls
through to the Needs Human queue. No data is lost, HR just clicks
through manually as a fallback.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import re
from html import unescape
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

# Avanan v2 wraps the destination URL between two ___ separators in the
# path, e.g. https://url.avanan.click/v2/r01/___<dest>___.<base64-sig>
_AVANAN_INLINE_DEST_PATTERN = re.compile(r"___(.*?)___", re.DOTALL)

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


def _unwrap_avanan_inline(url: str) -> str | None:
    """Avanan v2 URLs embed the real destination URL inline between two
    '___' separators in the path. Extract it without any HTTP call.
    Returns the destination URL, or None if the wrapper doesn't match
    the inline format (in which case the caller can fall back to
    HTTP redirect-following)."""
    if "url.avanan.click" not in url.lower():
        return None
    m = _AVANAN_INLINE_DEST_PATTERN.search(url)
    if not m:
        return None
    dest = unescape(m.group(1)).strip()
    # The path may contain '&amp;' from HTML email encoding, and may
    # have URL-encoding from the email body's char set. Try to clean.
    if dest.startswith("http"):
        return dest
    return None


def _unwrap_avanan(url: str) -> str | None:
    """If url is an Avanan-wrapped URL, recover the real destination.
    First tries the inline extraction (no HTTP). Falls back to following
    the redirect chain via requests if the inline format doesn't match.
    Handles the cts.indeed.com hop by decoding its gzip-base64-JSON
    path blob locally."""
    if "url.avanan.click" not in url.lower():
        return url
    # Inline extraction: no HTTP call needed.
    inline = _unwrap_avanan_inline(url)
    candidate_url = inline
    if not candidate_url:
        # Fall back to HTTP redirect-following.
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": _USER_AGENT},
                timeout=_FETCH_TIMEOUT_SECONDS,
                allow_redirects=True,
                stream=True,
            )
            candidate_url = resp.url
            resp.close()
        except requests.RequestException as e:
            log.warning("indeed_fetcher: avanan unwrap failed: %s", type(e).__name__)
            return None
    host = (urlparse(candidate_url or "").hostname or "").lower()
    if "employers.indeed.com" in host:
        return candidate_url
    if host.endswith("indeed.com"):
        # cts.indeed.com or another click-tracking subdomain. The real
        # destination URL is encoded in the path; decode locally.
        unwrapped = _recover_indeed_url_from_cts(candidate_url)
        if unwrapped:
            return unwrapped
        # Diagnostic: log path segments and first ~80 chars of the
        # path blob so we can see why recovery failed. (URL itself is
        # not PII -- it just contains an Indeed token.)
        try:
            _path_segs = [s for s in urlparse(candidate_url).path.split("/") if s]
            _blob_preview = (_path_segs[1][:80] if len(_path_segs) >= 2 else "")
            log.warning(
                "indeed_fetcher: avanan -> %s recovery failed; path_segs=%d "
                "blob_preview=%r",
                host, len(_path_segs), _blob_preview,
            )
        except Exception:
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
    URL. Indeed's CTS v1 encodes the destination URL as a JSON blob in
    the path: https://cts.indeed.com/v1/<gzip-base64-json>/<signature>.
    The first path segment after /v1/ is gzip-compressed JSON (URL-safe
    base64 encoded) with a 'u' field containing the destination URL.

    Falls back to query-param extraction for older or alternative CTS
    formats."""
    try:
        parsed = urlparse(cts_url)
    except Exception:
        return None
    # Path-blob decode (current CTS v1 format).
    if parsed.path.startswith("/v1/"):
        segments = [s for s in parsed.path.split("/") if s]
        # segments[0] = 'v1', segments[1] = gzip-base64-blob
        if len(segments) >= 2:
            blob = segments[1]
            decoded = _try_gzip_b64_json(blob)
            if decoded and "employers.indeed.com" in decoded:
                # HTML-entity-decode in case the JSON had &amp;
                return unescape(decoded)
    # Query-param fallback for older CTS variants.
    try:
        params = parse_qs(parsed.query)
    except Exception:
        params = {}
    candidate_keys = ("p", "rdr", "redirectUrl", "url", "r", "target", "to")
    for key in candidate_keys:
        for raw_val in params.get(key, []):
            for decoded in (raw_val, unquote(raw_val), unquote(unquote(raw_val))):
                if "employers.indeed.com" in decoded:
                    return unescape(decoded)
            try:
                b64 = base64.urlsafe_b64decode(raw_val + "==").decode(
                    "utf-8", errors="ignore"
                )
                if "employers.indeed.com" in b64:
                    return unescape(b64.split()[0])
            except Exception:
                pass
    # Last-ditch: cts URL may have id= directly.
    for raw_val in params.get("id", []):
        if raw_val:
            return f"https://employers.indeed.com/candidates/resume?id={raw_val}"
    return None


def _try_gzip_b64_json(blob: str) -> str | None:
    """Decode a URL-safe base64-encoded gzip-compressed JSON blob and
    return the 'u' field if it points to an Indeed URL. Returns None
    if any step fails."""
    if not blob:
        return None
    try:
        # URL-safe base64 may be missing padding; add up to 3 '='.
        padded = blob + "=" * (-len(blob) % 4)
        raw = base64.urlsafe_b64decode(padded)
        if not raw.startswith(b"\x1f\x8b"):
            return None  # not gzip magic
        json_bytes = gzip.decompress(raw)
        obj = json.loads(json_bytes.decode("utf-8", errors="replace"))
    except Exception:
        return None
    if isinstance(obj, dict):
        # Indeed CTS v1 uses 'u' for the destination URL.
        for key in ("u", "url", "target", "rdr"):
            val = obj.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
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
