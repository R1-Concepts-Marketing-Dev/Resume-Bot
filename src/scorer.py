"""Score a resume against the active filters using Claude."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import anthropic

log = logging.getLogger(__name__)

# Ordered worst -> best. Used internally for sorting and for the deterministic
# overall_decision logic. The model never sees these numbers; they're just so
# we can compare categories in Python.
FIT_LEVELS = ("no_fit", "weak", "borderline", "strong", "excellent")
_LEVEL_RANK = {lvl: i for i, lvl in enumerate(FIT_LEVELS)}

# Levels that count as "qualified" for the overall decision.
QUALIFIED_LEVELS = {"strong", "excellent"}
# Levels included on the dashboard's best_fit_roles list (everything except
# no_fit). "weak" shows up so HR can see roles the bot considered and rejected.
SURFACED_LEVELS = {"weak", "borderline", "strong", "excellent"}


SYSTEM_PROMPT = """You are an HR screening assistant for a brake parts \
company. You read a candidate's resume text (plus the email they sent it in) \
and decide how well they match each open role.

You always reply with a single JSON object and nothing else. No prose, no \
markdown, no backticks. The JSON must conform exactly to the schema in the \
user message. If the resume is unreadable or you cannot reasonably score it, \
set overall_decision to "needs_review" and explain in reasoning.

If the document is NOT a candidate resume at all -- e.g. a company \
newsletter, internal HR communication, security alert, drive-share \
notification, marketing/sales pitch, automated bounce, or any other \
non-application document -- set overall_decision to "not_a_resume", return \
an empty best_fit_roles array, and explain in reasoning what the document \
actually is."""


USER_TEMPLATE = """Today's date: {today}. Use this as your anchor for "recent" and "current." Any "Present" or "Current" in the resume means up through today. Treat all date math relative to today, not your training cutoff.

Open roles and their minimum requirements:

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
      "fit_level": "excellent" | "strong" | "borderline" | "weak" | "no_fit",
      "reasoning": "<1-2 sentence justification of the level>"
    }}
  ],
  "overall_decision": "qualified" | "not_qualified" | "needs_review" | "not_a_resume",
  "years_relevant_experience": <number, 0 if unknown>,
  "job_hopping_flag": "positive" | "caution" | "neutral",
  "reasoning": "<2-4 sentence overall explanation HR will read>",
  "confidence": <number between 0 and 1>,
  "candidate_name": "<best guess or empty string>",
  "candidate_email": "<best guess or empty string>",
  "candidate_phone": "<best guess or empty string>",
  "applied_for_role": "<exact role name the applicant explicitly applied for, OR 'unspecified'>"
}}

Include EVERY role in best_fit_roles where the candidate is at least "weak" -- i.e. they were considered for the role even if they fall short. Omit only roles where they are clearly "no_fit". Sort by fit_level descending (excellent > strong > borderline > weak).

Fit-level rubric (apply to each role independently):
  excellent   Meets the minimum requirement AND has extra relevant experience, recent employment, and a stable tenure pattern. Top-of-funnel hire.
  strong      Meets the minimum requirement comfortably. Recent and verifiable. Would interview.
  borderline  Meets the minimum but only just -- limited evidence, unclear tenure, or shaky recency. HR should decide.
  weak        Close but does NOT meet the stated minimum. Some adjacent experience, not enough on the actual requirement.
  no_fit      Clearly not a match for this role. Omit from best_fit_roles.

overall_decision rules:
  qualified       - at least one role is "excellent" or "strong"
  not_qualified   - no role is "borderline" or higher
  needs_review    - anything ambiguous, OCR-degraded, or confidence < 0.6
  not_a_resume    - the document is not a candidate resume at all (see Not-a-resume rule below)

Not-a-resume rule: BEFORE applying any of the rules below, look at what the document actually is. If it is NOT a candidate resume -- e.g. a company newsletter, internal memo, security alert (Google account warnings, Drive share notifications, calendar invites), bounced-message report, marketing/sales pitch, automated transactional email, vendor outreach, or anything else that is not a person submitting their work history for a job -- set overall_decision to "not_a_resume", return an empty best_fit_roles list, set applied_for_role to "unspecified", set years_relevant_experience to 0, and explain in reasoning what the document actually is and who likely sent it. Do NOT score it against the open roles. This decision overrides every other rule below.

Email-context rule: if the applicant's email subject or body explicitly mentions a specific role, prioritize that role in your assessment. If they don't specify, evaluate against every open role.

applied_for_role rule: read the email subject and body. If the applicant explicitly names a role they're applying for (e.g. "applying for Cherry Picker", "interested in the forklift driver position"), match it to the closest role name from the list above and return that EXACT name. If they just say "any position", "warehouse work", or don't mention a role at all, return "unspecified".

Verifiability rule: a fit_level of "strong" or "excellent" requires the resume to provide at least one verifiable employer name AND a date range (e.g. "2022-2024 at ABC Logistics" or "Mar 2023 - Present, FastWarehouse Inc"). If experience claims have no employer name or no dates, cap fit_level at "borderline" for every role and set overall_decision to "needs_review", regardless of how plausible the claims sound. Vague resumes that just list years of experience without specifics are not enough.

Applied-for trump rule: if applied_for_role is NOT "unspecified", the overall_decision MUST be driven by the fit_level for THAT specific role:
  - applied role is "strong" or "excellent" -> overall_decision can be "qualified"
  - applied role is "borderline" -> overall_decision must be "needs_review"
  - applied role is "weak" or "no_fit" -> overall_decision must be "not_qualified"
Cross-fit levels for OTHER roles are informational only when the applicant specified what they wanted. Do not "upgrade" the overall_decision based on a high level in a role they did not apply for. The applicant chose a role; respect that choice for the decision.

Recency rule: identify the end date of the candidate's most recent work experience. ONLY trigger this rule if that end date is MORE THAN 12 months before today. A 7-month gap or a 10-month gap does NOT trigger this rule -- only gaps strictly longer than 12 months. If the gap is more than 12 months, cap fit_level at "borderline" for ALL roles and set overall_decision to "needs_review". Skills decay - someone who hasn't worked in 18 months is not the same hire as someone working through last week. Currently-employed candidates (current or "Present" end date) are NEVER affected by this rule.

Job-hopping hard cap: look at the candidate's last 18 months of work history relative to today's date (provided above). Count the roles that started and/or ended within that window. If 3 or more of those roles each lasted less than 9 months, cap fit_level at "borderline" for all roles. This is a pattern test, not an all-or-nothing test: even if the candidate has one longer role mixed in (e.g. 11 months at one employer surrounded by 3-month stints at others), the surrounding pattern of short tenures still triggers the cap. The point is recent flight risk -- someone with three 2-4 month roles in the last year is at high risk of leaving regardless of what came before.

Concurrent-role exception: before counting roles for the job-hopping cap, check for overlapping date ranges. If two or more roles in the resume have dates that overlap in time (e.g. "TW Services Jan 2021-Present" running alongside "Harbor Logistics Nov 2025-Jan 2026"), identify the longest-running role as the primary employment and treat any overlapping shorter roles as side gigs, temp/contract work, or second jobs -- NOT as job changes. When applying the job-hopping cap, do not count these overlapping side gigs as separate "hops"; only count roles that represent a true switch of primary employment (one role ending before another begins). Side gigs alongside stable primary employment are a sign of work ethic, not flight risk. A candidate with one stable 5-year job and three concurrent 2-month temp gigs is NOT a job-hopper.
"""


def classify_no_resume_intent(*, api_key: str, subject: str, body: str) -> str:
    """For an email that has no resume attached, decide if the sender is
    (a) APPLYING (likely forgot to attach the resume) or
    (b) asking a QUESTION (about pay / hours / open positions / process).

    Returns 'application' or 'question'. Defaults to 'question' on any
    error -- the question reply is more generic and safer to send."""
    if not (subject or body):
        return "question"
    user_msg = (
        f"Subject: {subject or '(none)'}\n\n"
        f"Body:\n{(body or '(empty)')[:1500]}"
    )
    system = (
        "You classify a single email's intent. Reply with exactly one word: "
        "APPLICATION or QUESTION.\n\n"
        "APPLICATION = the sender is applying for a job at this company. "
        "Signals: mentions 'applying for', 'interested in the position', "
        "'please find my resume attached', names a specific role, attaches "
        "a cover letter, etc. Likely forgot to attach the resume.\n\n"
        "QUESTION = the sender is asking about open positions, pay, "
        "scheduling, application process, hours, benefits, or anything "
        "else needing a human response. They are NOT submitting an "
        "application.\n\n"
        "If genuinely ambiguous, reply QUESTION."
    )
    try:
        client = anthropic.Anthropic(api_key=api_key)
        # Use Haiku for this tiny classification call -- cheap and fast.
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=10,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        text = text.strip().upper()
        if "APPLICATION" in text and "QUESTION" not in text:
            return "application"
        return "question"
    except Exception as e:
        log.warning("Intent classifier failed, defaulting to question: %s", e)
        return "question"


def _build_filter_block(filters: list) -> str:
    return "\n".join(f"- {f.role} - {f.requirement}" for f in filters)


def score(*, api_key: str, model: str, resume_text: str, filters: list,
          email_subject: str = "", email_body: str = "",
          used_ocr: bool = False) -> dict[str, Any]:
    if not resume_text.strip():
        return _fallback("Empty resume text - could not extract content.")

    job_hopping = filters[0].job_hopping if filters else "Average tenure > 1 year = positive"
    from datetime import date
    user_msg = USER_TEMPLATE.format(
        today=date.today().isoformat(),
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


def _coerce_level(raw: Any) -> str:
    """Map any incoming value to one of the FIT_LEVELS, defaulting to no_fit."""
    if isinstance(raw, str):
        s = raw.strip().lower().replace("-", "_").replace(" ", "_")
        if s in _LEVEL_RANK:
            return s
        # Backwards-compatible: if the model emits a numeric string, bridge it.
        try:
            n = int(float(s))
            return _score_to_level(n)
        except ValueError:
            return "no_fit"
    if isinstance(raw, (int, float)):
        return _score_to_level(int(raw))
    return "no_fit"


def _score_to_level(n: int) -> str:
    """Legacy bridge: map an old 0-100 score to a fit_level bucket."""
    if n >= 90:
        return "excellent"
    if n >= 70:
        return "strong"
    if n >= 50:
        return "borderline"
    if n >= 30:
        return "weak"
    return "no_fit"


def _normalize(r: dict[str, Any]) -> dict[str, Any]:
    decision = r.get("overall_decision")
    if decision not in {"qualified", "not_qualified", "needs_review", "not_a_resume"}:
        r["overall_decision"] = "needs_review"

    # not_a_resume short-circuits everything. No role scoring, no trump rule.
    if r["overall_decision"] == "not_a_resume":
        r["best_fit_roles"] = []
        r["applied_for_role"] = "unspecified"
        r["years_relevant_experience"] = 0
        return r

    cleaned = []
    for item in r.get("best_fit_roles") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip()
        if not role:
            continue
        # Accept either fit_level (new) or fit_score (legacy) from the model.
        if "fit_level" in item:
            level = _coerce_level(item.get("fit_level"))
        else:
            level = _coerce_level(item.get("fit_score"))
        # Drop roles the model flagged as no_fit -- they shouldn't be on the list.
        if level == "no_fit":
            continue
        cleaned.append({
            "role": role,
            "fit_level": level,
            "reasoning": str(item.get("reasoning", "")).strip(),
        })
    cleaned.sort(key=lambda x: _LEVEL_RANK[x["fit_level"]], reverse=True)
    r["best_fit_roles"] = cleaned

    # Applied-for trump rule: derive overall_decision from the candidate's
    # fit on the role they actually applied to, when present. Otherwise
    # fall back to the model's own decision.
    applied_for = str(r.get("applied_for_role", "") or "").strip()
    if applied_for and applied_for.lower() != "unspecified":
        applied_level = "no_fit"
        for item in cleaned:
            if item["role"].strip().lower() == applied_for.lower():
                applied_level = item["fit_level"]
                break
        if applied_level in QUALIFIED_LEVELS:
            r["overall_decision"] = "qualified"
        elif applied_level == "borderline":
            r["overall_decision"] = "needs_review"
        else:
            r["overall_decision"] = "not_qualified"

    return r
