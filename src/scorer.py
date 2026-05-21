"""Score a resume against the active filters using Claude."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import anthropic

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an HR screening assistant for a brake parts \
company. You read a candidate's resume text (plus the email they sent it in) \
and decide how well they match each open role.

You always reply with a single JSON object and nothing else. No prose, no \
markdown, no backticks. The JSON must conform exactly to the schema in the \
user message. If the resume is unreadable or you cannot reasonably score it, \
set overall_decision to "needs_review" and explain in reasoning."""


USER_TEMPLATE = """Open roles and their minimum requirements:

{filter_block}

Job-hopping guidance (applies to all roles): {job_hopping}

---

Email subject from the applicant: {email_subject}
Email body from the applicant (may be empty):
{email_body}

---

Resume text (may be OCR'd; some noise is expected):
{resume_text}

---

Respond with this exact JSON schema:

{{
  "best_fit_roles": [
    {{
      "role": "<exact role name from the list above>",
      "fit_score": <integer 0-100>,
      "reasoning": "<1-2 sentence justification of the score>"
    }}
  ],
  "overall_decision": "qualified" | "not_qualified" | "needs_review",
  "years_relevant_experience": <number, 0 if unknown>,
  "job_hopping_flag": "positive" | "caution" | "neutral",
  "reasoning": "<2-4 sentence overall explanation HR will read>",
  "confidence": <number between 0 and 1>,
  "candidate_name": "<best guess or empty string>",
  "candidate_email": "<best guess or empty string>",
  "candidate_phone": "<best guess or empty string>",
  "applied_for_role": "<exact role name the applicant explicitly applied for, OR 'unspecified'>"
}}

Include EVERY role from the list above where fit_score >= 30 in best_fit_roles. Omit roles where the candidate is clearly not a fit at all. Sort by fit_score descending.

Fit-score rubric (apply to each role independently):
  90-100  Strong match: meets minimum + extra relevant experience + recent + good tenure
  70-89   Meets the minimum bar comfortably
  50-69   Meets the minimum but borderline
  30-49   Close but does not meet the stated minimum
  0-29    Clearly not a fit (omit from best_fit_roles)

overall_decision rules:
  qualified       - at least one role has fit_score >= 60
  not_qualified   - no role has fit_score >= 50
  needs_review    - anything ambiguous, OCR-degraded, or confidence < 0.6

Email-context rule: if the applicant's email subject or body explicitly mentions a specific role, prioritize that role in your scoring. If they don't specify, score against every open role.

applied_for_role rule: read the email subject and body. If the applicant explicitly names a role they're applying for (e.g. "applying for Cherry Picker", "interested in the forklift driver position"), match it to the closest role name from the list above and return that EXACT name. If they just say "any position", "warehouse work", or don't mention a role at all, return "unspecified".
"""


def _build_filter_block(filters: list) -> str:
    return "\n".join(f"- {f.role} - {f.requirement}" for f in filters)


def score(*, api_key: str, model: str, resume_text: str, filters: list,
          email_subject: str = "", email_body: str = "",
          used_ocr: bool = False) -> dict[str, Any]:
    if not resume_text.strip():
        return _fallback("Empty resume text - could not extract content.")

    job_hopping = filters[0].job_hopping if filters else "Average tenure > 1 year = positive"
    user_msg = USER_TEMPLATE.format(
        filter_block=_build_filter_block(filters),
        job_hopping=job_hopping,
        email_subject=(email_subject or "(none)")[:200],
        email_body=(email_body or "(empty)")[:2000],
        resume_text=resume_text[:18000],
    )

    if used_ocr:
        user_msg += "\n\nNOTE: This resume was OCR'd from a scanned PDF. Expect typos and broken layout."

    client = anthropic.Anthropic(api_key=api_key)
    try:
        resp = client.messages.create(
            model=model, max_tokens=3000, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        log.exception("Claude API call failed")
        return _fallback(f"Claude API error: {e}")

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()

    # Strip markdown code fences if Claude wrapped the JSON (it often does
    # despite the system prompt's instruction not to).
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|JSON)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Claude returned non-JSON: %s", text[:500])
        return _fallback("Scorer returned non-JSON response.")

    return _normalize(result)


def _fallback(reason: str) -> dict[str, Any]:
    return {
        "best_fit_roles": [],
        "overall_decision": "needs_review",
        "years_relevant_experience": 0,
        "job_hopping_flag": "neutral",
        "reasoning": reason,
        "confidence": 0.0,
        "candidate_name": "",
        "candidate_email": "",
        "candidate_phone": "",
        "applied_for_role": "unspecified",
    }


def _normalize(r: dict[str, Any]) -> dict[str, Any]:
    decision = r.get("overall_decision")
    if decision not in {"qualified", "not_qualified", "needs_review"}:
        r["overall_decision"] = "needs_review"

    cleaned = []
    for item in r.get("best_fit_roles") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip()
        try:
            sc = int(item.get("fit_score", 0))
        except (TypeError, ValueError):
            sc = 0
        sc = max(0, min(100, sc))
        if not role:
            continue
        cleaned.append({
            "role": role,
            "fit_score": sc,
            "reasoning": str(item.get("reasoning", "")).strip(),
        })
    cleaned.sort(key=lambda x: x["fit_score"], reverse=True)
    r["best_fit_roles"] = cleaned

    r.setdefault("years_relevant_experience", 0)
    r.setdefault("job_hopping_flag", "neutral")
    r.setdefault("reasoning", "")
    r.setdefault("confidence", 0)
    r.setdefault("candidate_name", "")
    r.setdefault("candidate_email", "")
    r.setdefault("candidate_phone", "")
    r.setdefault("applied_for_role", "unspecified")
    if not str(r["applied_for_role"]).strip():
        r["applied_for_role"] = "unspecified"
    return r
