"""Fetch resumes from Indeed Quick Apply emails.

When a candidate applies via Indeed without uploading a PDF themselves,
the forwarded application email contains only a 'View resume' link to
Indeed's public resume viewer. This module follows that link's id token
to Indeed's public PDF download endpoint and returns the PDF bytes so
the bot can score the candidate the same way it would any other
applicant who attached a resume directly.

R1 Concepts uses Avanan (Check Point) email security, which rewrites
every URL to url.avanan.click/v2/<random>/___<dest>___.<sig>. We
extract the destination inline (no HTTP), then locally decode the
cts.indeed.com path blob (gzip-base64-JSON) to recover the real
employers.indeed.com URL with id token.
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


_VIEW_URL_PATTERN = re.compile(
    r"https?://employers\.indeed\.com/candidates/resume\?[^\s\"'<>]+",
    re.IGNORECASE,
)

_AVANAN_URL_PATTERN = re.compile(
    r"https?://url\.avanan\.click/v2/[^\s\"'<>]+",
    re.IGNORECASE,
)

_AVANAN_INLINE_DEST_PATTERN = re.compile(r"___(.*?)___", re.DOTALL)

_DOWNLOAD_ENDPOINT = "https://employers.indeed.com/api/catws/public/resume/download"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_FETCH_TIMEOUT_SECONDS = 15
_MAX_PDF_BYTES = 15 * 1024 * 1024  # 15 MB


def extract_view_resume_url(email_body: str) -> str | None:
    if not email_body:
        return None
    match = _VIEW_URL_PATTERN.search(email_body)
    if match:
        return match.group(0)
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
    """Avanan v2 URLs embed the destination URL inline between two '___'
    separators. Extract it without HTTP."""
    if "url.avanan.click" not in url.lower():
        return None
    m = _AVANAN_INLINE_DEST_PATTERN.search(url)
    if not m:
        return None
    dest = unescape(m.group(1)).strip()
    # Try URL-decoding in case the dest is %-encoded inside the wrapper.
    if not dest.startswith("http"):
        dest_decoded = unquote(dest)
        if dest_decoded.startswith("http"):
            dest = dest_decoded
    if dest.startswith("http"):
        return dest
    return None


def _unwrap_avanan(url: str) -> str | None:
    if "url.avanan.click" not in url.lower():
        return url
    inline = _unwrap_avanan_inline(url)
    candidate_url = inline
    if not candidate_url:
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
            log.warning("indeed_fetcher: avanan unwrap failed: %s url=%r",
                        type(e).__name__, url[:200])
            return None
    host = (urlparse(candidate_url or "").hostname or "").lower()
    if "employers.indeed.com" in host:
        return candidate_url
    if host.endswith("indeed.com"):
        unwrapped = _recover_indeed_url_from_cts(candidate_url)
        if unwrapped:
            return unwrapped
        # Diagnostic: log first path segment AND the source url so we can see why decode failed.
        try:
            _segs = [s for s in urlparse(candidate_url).path.split("/") if s]
            _blob = _segs[1][:80] if len(_segs) >= 2 else ""
            log.warning(
                "indeed_fetcher: avanan -> %s recovery failed; segs=%d blob_head=%r cts_url=%r src=%r",
                host, len(_segs), _blob, (candidate_url or "")[:200], url[:200],
            )
        except Exception:
            log.warning("indeed_fetcher: avanan -> %s recovery failed src=%r", host, url[:200])
        return None
    log.warning(
        "indeed_fetcher: avanan redirect did not land on Indeed (host=%s) candidate=%r src=%r",
        host, (candidate_url or "")[:200], url[:200],
    )
    return None


def _recover_indeed_url_from_cts(cts_url: str) -> str | None:
    """Decode cts.indeed.com URL's gzip-base64-JSON path blob and return
    the real employers.indeed.com URL. Falls back to query-param
    extraction for older CTS variants."""
    try:
        parsed = urlparse(cts_url)
    except Exception:
        return None
    if parsed.path.startswith("/v1/"):
        segments = [s for s in parsed.path.split("/") if s]
        if len(segments) >= 2:
            blob = segments[1]
            decoded = _try_gzip_b64_json(blob)
            if decoded and "employers.indeed.com" in decoded:
                return unescape(decoded)
            # Try URL-decoding the blob in case it was %-encoded.
            blob2 = unquote(blob)
            if blob2 != blob:
                decoded2 = _try_gzip_b64_json(blob2)
                if decoded2 and "employers.indeed.com" in decoded2:
                    return unescape(decoded2)
    try:
        params = parse_qs(parsed.query)
    except Exception:
        params = {}
    candidate_keys = ("p", "rdr", "redirectUrl", "url", "r", "target", "to")
    for key in candidate_keys:
        for raw_val in params.get(key, []):
            for d in (raw_val, unquote(raw_val), unquote(unquote(raw_val))):
                if "employers.indeed.com" in d:
                    return unescape(d)
            try:
                b64 = base64.urlsafe_b64decode(raw_val + "==").decode(
                    "utf-8", errors="ignore"
                )
                if "employers.indeed.com" in b64:
                    return unescape(b64.split()[0])
            except Exception:
                pass
    for raw_val in params.get("id", []):
        if raw_val:
            return f"https://employers.indeed.com/candidates/resume?id={raw_val}"
    return None


def _try_gzip_b64_json(blob: str) -> str | None:
    if not blob:
        return None
    try:
        padded = blob + "=" * (-len(blob) % 4)
        raw = base64.urlsafe_b64decode(padded)
        if not raw.startswith(b"\x1f\x8b"):
            return None
        json_bytes = gzip.decompress(raw)
        obj = json.loads(json_bytes.decode("utf-8", errors="replace"))
    except Exception:
        return None
    if isinstance(obj, dict):
        for key in ("u", "url", "target", "rdr"):
            val = obj.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
    return None


def build_download_url(view_resume_url: str) -> str | None:
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
    download_url = build_download_url(view_resume_url)
    if not download_url:
        log.warning("indeed_fetcher: could not build download URL from view URL=%r",
                    (view_resume_url or "")[:200])
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
        log.warning("indeed_fetcher: request failed: %s download_url=%r",
                    type(e).__name__, download_url)
        return None
    if resp.status_code != 200:
        log.warning("indeed_fetcher: HTTP %d from Indeed download_url=%r body_head=%r",
                    resp.status_code, download_url, (resp.text or "")[:300])
        return None
    content_type = (resp.headers.get("content-type") or "").lower()
    if "pdf" not in content_type:
        log.warning("indeed_fetcher: unexpected content-type=%r download_url=%r body_head=%r",
                    content_type, download_url, (resp.text or "")[:300])
        return None
    body = resp.content
    if not body:
        log.warning("indeed_fetcher: empty response body download_url=%r", download_url)
        return None
    if not body.startswith(b"%PDF"):
        log.warning("indeed_fetcher: response body is not a PDF (first=%r) download_url=%r",
                    body[:8], download_url)
        return None
    if len(body) > _MAX_PDF_BYTES:
        log.warning("indeed_fetcher: PDF too large (%d bytes)", len(body))
        return None
    log.info("indeed_fetcher: fetched %d-byte PDF from Indeed", len(body))
    return body
