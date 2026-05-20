"""Score a resume against the active filters using Claude.

Returns a strict JSON dict the orchestrator uses to decide:
- which Drive folder to file the resume into
- what to write to the dashboard
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an HR screening assistant for R1 Concepts, a brake \
parts company. You read a candidate's resume text and decide whether they meet \
any of the company's active role filters.

You always reply with a single JSON object and nothing else. No prose, no \
markdown, no backticks. The JSON must conform exactly to the schema you are \
given. If the resume is unreadable, badly OCR'd, or you cannot reasonably \
determine fit, set bucket to "needs_review" and explain in the reasoning \
field. Prefer "needs_review" over a confident wrong answer."""

USER_TEMPLATE = """Active role filters (each line is one role and its minimum \
requirement):

{filter_block}

Job hopping guidance: {job_hopping}

---

Resume text (may be OCR'd; some noise is expected):

{resume_text}

---

Respond with this exact JSON schema:

{{
  "bucket": "qualified" | "not_qualified" | "needs_review",
  "best_fit_roles": ["role name", ...],   // empty array if none
  "years_relevant_experience": <number, 0 if unknown>,
  "job_hopping_flag": "positive" | "caution" | "neutral",
  "reasoning": "<2-4 sentence explanation HR will read>",
  "confidence": <number between 0 and 1>,
  "candidate_name": "<best guess or empty string>",
  "candidate_email": "<best guess or empty string>",
  "candidate_phone": "<best guess or empty string>"
}}

Rules:
- "qualified" requires meeting the explicit minimum requirement for at least \
one active role.
- "not_qualified" means clearly not meeting any role's minimum.
- "needs_review" for ambiguous, missing-info, or OCR-degraded resumes.
- best_fit_roles lists role names from the filters above that the candidate \
plausibly fits.
- If confidence < 0.6, set bucket to "needs_review" regardless of fit.
"""


def _build_filter_block(filters: list) -> str:
    lines = []
    for f in filters:
        lines.append(f"- {f.role} — {f.requirement}")
    return "\n".join(lines)


def score(api_key: str, model: str, resume_text: str, filters: list,
          used_ocr: bool = False) -> dict[str, Any]:
    """Score a single resume. Returns the parsed JSON dict from Claude.

    Falls back to {"bucket": "needs_review", ...} on any failure so the bot
    never silently drops a candidate.
    """
    if not resume_text.strip():
        return _fallback("Empty resume text — could not extract content.")

    job_hopping = filters[0].job_hopping if filters else "Average tenure > 1 year = positive"
    user_msg = USER_TEMPLATE.format(
        filter_block=_build_filter_block(filters),
        job_hopping=job_hopping,
        resume_text=resume_text[:18000],  # keep tokens bounded; resumes >18k chars are very rare
    )

    if used_ocr:
        user_msg += "\n\nNOTE: This resume was OCR'd from a scanned PDF. Expect typos and broken layout."

    client = anthropic.Anthropic(api_key=api_key)
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        log.exception("Claude API call failed")
        return _fallback(f"Claude API error: {e}")

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Claude returned non-JSON: %s", text[:500])
        return _fallback("Scorer returned non-JSON response.")

    return _normalize(result)


def _fallback(reason: str) -> dict[str, Any]:
    return {
        "bucket": "needs_review",
        "best_fit_roles": [],
        "years_relevant_experience": 0,
        "job_hopping_flag": "neutral",
        "reasoning": reason,
        "confidence": 0.0,
        "candidate_name": "",
        "candidate_email": "",
        "candidate_phone": "",
    }


def _normalize(r: dict[str, Any]) -> dict[str, Any]:
    bucket = r.get("bucket")
    if bucket not in {"qualified", "not_qualified", "needs_review"}:
        r["bucket"] = "needs_review"
    r.setdefault("best_fit_roles", [])
    r.setdefault("years_relevant_experience", 0)
    r.setdefault("job_hopping_flag", "neutral")
    r.setdefault("reasoning", "")
    r.setdefault("confidence", 0)
    r.setdefault("candidate_name", "")
    r.setdefault("candidate_email", "")
    r.setdefault("candidate_phone", "")
    return r
