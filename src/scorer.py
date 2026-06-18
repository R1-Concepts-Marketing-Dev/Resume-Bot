"""Score a resume against the active filters using Claude."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import anthropic

log = logging.getLogger(__name__)

FIT_LEVELS = ("no_fit", "weak", "borderline", "strong", "excellent")
_LEVEL_RANK = {lvl: i for i, lvl in enumerate(FIT_LEVELS)}

QUALIFIED_LEVELS = {"strong", "excellent"}
SURFACED_LEVELS = {"weak", "borderline", "strong", "excellent"}


@dataclass(frozen=True)
class ClassifierResult:
    """Output of classify_inbound_email: the routing label plus the
    model's confidence in that label. Confidence is used to escalate
    ambiguous cases to the Needs Human review queue."""
    label: str          # "resume" | "application_no_resume" | "question" | "misc"
    confidence: float   # 0.0 - 1.0


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
  "applied_for_role": "<exact role name the applicant explicitly applied for, OR 'unspecified'>",
  "recruiter_agency": "<recruiter or staffing agency name if the email was sent by a third-party recruiter representing this candidate, OR 'N/A' if the candidate is applying directly>"
}}

Include EVERY role in best_fit_roles where the candidate is at least "weak" -- i.e. they were considered for the role even if they fall short. Omit only roles where they are clearly "no_fit". Sort by fit_level descending (excellent > strong > borderline > weak).

Fit-level rubric (apply to each role independently):
  excellent   Meets the minimum requirement AND has extra relevant experience, recent employment, and a stable tenure pattern. Top-of-funnel hire.
  strong      Meets the minimum requirement comfortably. Recent and verifiable. Would interview.
  borderline  Demonstrably meets the stated minimum, but only just -- e.g. exactly meets the years-of-experience floor with no buffer, or tenure timing is murky on the resume. The minimum requirement IS satisfied; only the confidence level is shaky.
  weak        Does NOT meet the stated minimum on a specific, measurable axis. Examples: role requires 1+ year cherry picker experience and the candidate has 6 months -> weak. Role requires forklift certification and the resume shows no cert -> weak. Role requires "warehouse experience" and the candidate's work history is retail, food service, or PC repair with no warehouse work -> weak. Adjacent or "implied" experience does NOT bridge an explicit minimum -- if the resume only says "general labor" or "logistics" when the requirement is "cherry picker", that is weak, not borderline. Maps to overall_decision = not_qualified.
  no_fit      Clearly not a match for this role at all (wrong industry, no relevant skills). Omit from best_fit_roles.

CRITICAL: requirements are requirements, not nice-to-haves. If the role spec says "1+ year cherry picker experience" and the resume shows 6 months, that is WEAK -- the candidate falls short of a stated minimum. Do not promote them to borderline because their warehouse experience "looks close." Adjacent skills are not a substitute for the specific requirement listed.

overall_decision rules:
  qualified       - at least one role is "excellent" or "strong"
  not_qualified   - no role is "borderline" or higher
  needs_review    - anything ambiguous, OCR-degraded, or confidence < 0.6
  not_a_resume    - the document is not a candidate resume at all (see Not-a-resume rule below)

Not-a-resume rule: BEFORE applying any of the rules below, look at what the document actually is. If it is NOT a candidate resume -- e.g. a company newsletter, internal memo, security alert (Google account warnings, Drive share notifications, calendar invites), bounced-message report, marketing/sales pitch, automated transactional email, vendor outreach, or anything else that is not a person submitting their work history for a job -- set overall_decision to "not_a_resume", return an empty best_fit_roles list, set applied_for_role to "unspecified", set years_relevant_experience to 0, and explain in reasoning what the document actually is and who likely sent it. Do NOT score it against the open roles. This decision overrides every other rule below.

Email-context rule: if the applicant's email subject or body explicitly mentions a specific role, prioritize that role in your assessment. If they don't specify, evaluate against every open role.

applied_for_role rule: read the email subject and body. If the applicant explicitly names a role they're applying for (e.g. "applying for Cherry Picker", "interested in the forklift driver position"), match it to the closest role name from the list above and return that EXACT name. If they just say "any position", "warehouse work", or don't mention a role at all, return "unspecified".

recruiter_agency rule: read the email (not the resume). If the email was clearly sent by a third-party recruiter or staffing agency on behalf of the candidate (the sender is NOT the candidate themselves -- they write things like "I have a candidate for you", "attached is the resume of [name] who I am representing", "our staffing firm is submitting", or have a recruiter/agency signature like "ABC Staffing"), return the agency name as best you can extract it from the signature, body, or sender domain (e.g. "ABC Staffing", "TalentBridge Recruiting", "Robert Half"). If you can identify the recruiter as a person but not the agency, return their name and title (e.g. "Sarah Chen, Independent Recruiter"). If the candidate is applying directly themselves (first-person language, no agency framing), return "N/A". Do NOT classify a candidate writing their own application as recruiter outreach just because they mention past recruiter work.

candidate_email rule: extract the candidate's REAL email address. Look in this order: (1) the resume body itself, (2) the email body's structured contact fields (job boards like Indeed include a "Contact information" or "Email:" section), (3) the From: header. WARNING about job-board aliases: when an application is forwarded through Indeed, ZipRecruiter, or a similar job board, the From: header is an opaque alias like apply+abc123@indeed.com -- this is NOT the candidate's real email and should NEVER be returned here. If the email is clearly forwarded by a job board (sender domain is indeed.com, indeedemail.com, ziprecruiter.com, glassdoor.com, monster.com, careerbuilder.com, etc.) and you cannot find a real email in the resume or the body, return an empty string rather than the alias. Same rule for candidate_phone -- never return the job board's relay number.

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


# Few-shot block prepended to the user message when HR has approved
# learning examples on the Bot Learning Log tab. Frames the examples
# as HR corrections so the model knows to apply the same correction
# pattern, not just mimic the example's exact wording.
LEARNING_EXAMPLES_PREAMBLE = """PRIOR HR CORRECTIONS (recent disagreements where HR overrode the bot).
These are real cases from this company's pipeline where the bot's call did not match HR's final decision. HR's notes tell you why the bot was wrong. When you see a similar resume/position pattern below, apply the lesson HR left -- don't repeat the same mistake.

"""


INBOUND_TYPES = ("resume", "application_no_resume", "question", "misc")

# Lowercase string labels used internally + by main.py routing.
_LABEL_NORMALIZE = {
    "RESUME":                "resume",
    "APPLICATION_NO_RESUME": "application_no_resume",
    "QUESTION":              "question",
    "MISC":                  "misc",
}


@dataclass(frozen=True)
class ClosureResult:
    """Output of classify_conversation_closure: whether a candidate's reply
    signals the conversation has ended. Used to silently archive replies
    like 'thanks for letting me know' after a terminal template was sent,
    so the bot stops engaging with closed threads."""
    decision: str       # "closed" | "ongoing" | "unclear"
    confidence: float   # 0.0 - 1.0
    reasoning: str      # one short sentence


_CLOSURE_MODEL = "claude-haiku-4-5-20251001"


def classify_conversation_closure(*, api_key: str,
                                  bot_templates_sent: list[str],
                                  candidate_message_body: str,
                                  subject: str) -> ClosureResult:
    """Ask Claude whether the candidate's reply ends the conversation.

    Should only be called when the thread has prior bot activity (the
    bot has already sent at least one template). Returns ClosureResult.

    Defaults to ('unclear', 0.5) on any error -- safest behavior because
    main.py routes 'unclear' to Needs Human, so a transient API blip
    won't accidentally close a live thread."""
    if not candidate_message_body and not subject:
        return ClosureResult(decision="unclear", confidence=0.5,
                             reasoning="empty message body and subject")

    templates_list = ", ".join(bot_templates_sent) if bot_templates_sent else "(none)"
    user_msg = (
        f"The bot previously sent these auto-reply template(s) to this "
        f"candidate in the same email thread: {templates_list}.\n\n"
        f"The candidate has now replied.\n"
        f"Subject: {subject or '(none)'}\n\n"
        f"Body:\n{(candidate_message_body or '(empty)')[:1500]}"
    )
    system = (
        "You decide whether an inbound candidate email signals the natural "
        "end of a hiring conversation. Return ONLY a JSON object with this "
        "exact schema (no prose, no markdown, no code fences):\n"
        "  {\"decision\": \"closed|ongoing|unclear\", "
        "\"confidence\": <number 0.0-1.0>, "
        "\"reasoning\": \"<one short sentence>\"}\n\n"
        "WHAT COUNTS AS CLOSED\n"
        "A brief acknowledgment with no new substantive content. The "
        "candidate is signaling they have no further questions. Examples:\n"
        "  - 'Thanks for letting me know'\n"
        "  - 'Got it, appreciate the update'\n"
        "  - 'Understood, thank you'\n"
        "  - 'OK no problem, all the best'\n"
        "  - 'Thanks, will keep in touch'\n"
        "  - 'No worries, take care'\n\n"
        "WHAT COUNTS AS ONGOING\n"
        "The candidate is continuing the conversation in any substantive "
        "way:\n"
        "  - Asking a new or follow-up question\n"
        "  - Pushing back or asking us to reconsider\n"
        "  - Mentioning a different role they're now interested in\n"
        "  - Attaching a new or updated resume\n"
        "  - Providing additional context or qualifications\n"
        "  - Saying 'actually I'd like to discuss further'\n"
        "  - Anything that invites a substantive reply\n\n"
        "WHEN TO MARK UNCLEAR\n"
        "The reply is ambiguous -- partially closing but also asking "
        "something, or so terse you cannot tell intent. Mark unclear and a "
        "human will decide. When genuinely in doubt, prefer unclear over "
        "closed -- false positives here drop real candidates.\n\n"
        "Return ONLY the JSON object. No prose."
    )

    try:
        import anthropic
    except ImportError:
        return ClosureResult(decision="unclear", confidence=0.5,
                             reasoning="anthropic SDK unavailable")
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=_CLOSURE_MODEL,
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text).strip()
        data = json.loads(text)
    except Exception as e:
        log.warning("classify_conversation_closure failed: %s", e)
        return ClosureResult(decision="unclear", confidence=0.5,
                             reasoning=f"API error: {type(e).__name__}")

    decision = str(data.get("decision", "unclear")).strip().lower()
    if decision not in {"closed", "ongoing", "unclear"}:
        decision = "unclear"
    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    reasoning = str(data.get("reasoning", "") or "").strip()[:300]
    return ClosureResult(decision=decision, confidence=conf, reasoning=reasoning)


def classify_inbound_email(*, api_key: str, subject: str, body: str,
                           sender_email: str,
                           has_attachment: bool) -> ClassifierResult:
    """Classify an inbound jobs@ email and return (label, confidence)."""
    if not (subject or body or has_attachment):
        return ClassifierResult(label="question", confidence=0.5)

    user_msg = (
        f"From: {sender_email or '(unknown)'}\n"
        f"Has attachment: {'yes' if has_attachment else 'no'}\n"
        f"Subject: {subject or '(none)'}\n\n"
        f"Body:\n{(body or '(empty)')[:1500]}"
    )
    system = (
        "You triage a single inbound email to a jobs@ HR mailbox. Return "
        "ONLY a JSON object with this exact schema (no prose, no markdown, "
        "no code fences):\n"
        "  {\"label\": \"RESUME|APPLICATION_NO_RESUME|QUESTION|MISC\", "
        "\"confidence\": <number 0.0-1.0>}\n\n"
        "LANGUAGE\n"
        "--------\n"
        "The email may be written in any language. Apply the same rules and "
        "the same labels regardless of language. The bot's replies are always "
        "sent in English; your job is only to classify intent, not to match "
        "language.\n\n"
        "CATEGORY DEFINITIONS\n"
        "--------------------\n"
        "RESUME = the sender is a job applicant AND has_attachment is yes. "
        "An attachment is REQUIRED for this label. If has_attachment is no, "
        "never use RESUME no matter how the email reads.\n\n"
        "APPLICATION_NO_RESUME = the sender is applying for a job but "
        "did NOT attach a file. Signals (any of these is enough):\n"
        "  - 'I want to apply', 'I'm interested in the position', "
        "'please consider me'\n"
        "  - Their work history pasted into the body\n"
        "  - Subject names a specific role + the body introduces themselves\n"
        "  - Subject is mostly a phone number, a single first name, or "
        "another tiny fragment AND no attachment (likely a candidate who "
        "doesn't know how to attach; default to application intent)\n\n"
        "QUESTION = the sender is asking the HR team a SPECIFIC question "
        "and wants a human reply. Signals:\n"
        "  - 'is the X position still open?'\n"
        "  - 'what's the pay range?', 'what are the hours?'\n"
        "  - 'when do you interview?', 'where do I drop off my resume?'\n"
        "  - 'can I apply in person?'\n"
        "Important: an applicant who says 'I'm interested in X' is NOT "
        "asking a question -- that's APPLICATION_NO_RESUME. QUESTION "
        "requires an actual question being asked.\n\n"
        "MISC = NOT candidate-related at all. Signals:\n"
        "  - Newsletter, marketing/sales pitch from a vendor\n"
        "  - Automated notifications (no-reply@ addresses, platform mail)\n"
        "  - Internal company communications forwarded from a coworker\n"
        "  - Bounced-message reports, payroll service emails\n"
        "  - Body is almost entirely a copy of a job posting (sender is "
        "advertising the job, not applying to it)\n\n"
        "RECRUITER NOTE -- third-party recruiters and staffing agencies "
        "who attach a candidate's resume on their behalf are NOT misc. "
        "They count as RESUME (when an attachment is present) or "
        "APPLICATION_NO_RESUME (when there is no attachment). The bot "
        "still scores the candidate normally; the agency relationship "
        "is recorded separately in the scoring step, not here.\n\n"
        "DECISION ORDER -- check in order:\n"
        "1. no-reply / platform-domain sender -> MISC.\n"
        "2. Body reads as a copy of a job posting with no self-intro -> MISC.\n"
        "3. has_attachment is yes AND the email is about a candidate "
        "(self-intro OR recruiter pitching a candidate) -> RESUME.\n"
        "4. Application intent without an attachment (any "
        "APPLICATION_NO_RESUME signal, including recruiter outreach "
        "without a resume) -> APPLICATION_NO_RESUME.\n"
        "5. Specific question being asked, not applying -> QUESTION.\n"
        "6. Otherwise default to QUESTION.\n\n"
        "CONFIDENCE GUIDE\n"
        "----------------\n"
        "0.95+ = textbook example, all signals point one way (e.g. clear "
        "resume attachment + 'please find my resume attached')\n"
        "0.85-0.94 = strong signals one direction, no contradictions\n"
        "0.7-0.84 = correct call but some ambiguity (short body, "
        "unusual subject)\n"
        "0.5-0.69 = could go two ways, you're guessing the more likely one\n"
        "below 0.5 = genuinely unclear (you'd want a human to look)\n\n"
        "WORKED EXAMPLES\n"
        "---------------\n"
        "Example 1: subject 'Cherry Picker Operator', attachment yes, body "
        "'Please find my resume attached.' -> "
        "{\"label\":\"RESUME\",\"confidence\":0.97}\n\n"
        "Example 2: subject 'Warehouse Packer', attachment no, body "
        "'Hi, I'm really interested in this position.' -> "
        "{\"label\":\"APPLICATION_NO_RESUME\",\"confidence\":0.9}\n\n"
        "Example 3: subject '5551234567 Oscar', attachment no, body '' "
        "-> {\"label\":\"APPLICATION_NO_RESUME\",\"confidence\":0.75}\n\n"
        "Example 4: subject 'is the cherry picker job still open?', "
        "attachment no, body 'I saw it on indeed.' -> "
        "{\"label\":\"QUESTION\",\"confidence\":0.92}\n\n"
        "Example 5: subject 'Warehouse Packer Position', attachment no, "
        "body 'Warehouse Packer - $18/hour. Apply now at...' -> "
        "{\"label\":\"MISC\",\"confidence\":0.88}\n\n"
        "Example 6: from no-reply@indeed.com -> "
        "{\"label\":\"MISC\",\"confidence\":0.98}\n\n"
        "Example 7: subject 'Cherry Picker', attachment no, body 'Hi! "
        "I'm excited about this. I saw your post on Craigslist.' -> "
        "{\"label\":\"APPLICATION_NO_RESUME\",\"confidence\":0.85}\n\n"
        "Example 8: subject 'Strong candidate for your Forklift role', "
        "attachment yes (resume.pdf), body 'Hello, I'm a recruiter at XYZ "
        "Staffing. I have a candidate, John Smith, who would be a great fit "
        "for your warehouse opening. His resume is attached for your "
        "review.' -> {\"label\":\"RESUME\",\"confidence\":0.95}\n\n"
        "Example 9 (Spanish): subject 'Solicitud para Cherry Picker', "
        "attachment yes (curriculum.pdf), body 'Hola, adjunto mi curriculum "
        "para la posicion de Cherry Picker. Tengo 5 anos de experiencia. "
        "Gracias.' -> {\"label\":\"RESUME\",\"confidence\":0.95}\n\n"
        "Example 10 (Spanish, no attachment): subject 'Buen dia', "
        "attachment no, body 'Hola, estoy interesado en una posicion en su "
        "empresa.' -> {\"label\":\"APPLICATION_NO_RESUME\",\"confidence\":0.85}\n\n"
        "Return ONLY the JSON object. No prose."
    )
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=80,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text).strip()
        data = json.loads(text)
        label_raw = str(data.get("label", "")).strip().upper().rstrip(".,;:")
        label = _LABEL_NORMALIZE.get(label_raw)
        if not label:
            for k, v in _LABEL_NORMALIZE.items():
                if k in label_raw:
                    label = v
                    break
        if not label:
            log.warning("Classifier returned unknown label %r; defaulting", label_raw)
            return ClassifierResult(label="question", confidence=0.4)
        try:
            conf = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        return ClassifierResult(label=label, confidence=conf)
    except Exception as e:
        log.warning("Email classifier failed (%s); defaulting to question", e)
        return ClassifierResult(label="question", confidence=0.3)


def classify_no_resume_intent(*, api_key: str, subject: str, body: str) -> str:
    """Legacy 2-way classifier. Prefer classify_inbound_email."""
    result = classify_inbound_email(
        api_key=api_key, subject=subject, body=body,
        sender_email="", has_attachment=False,
    )
    if result.label == "application_no_resume":
        return "application"
    return "question"


def _build_filter_block(filters: list) -> str:
    return "\n".join(f"- {f.role} - {f.requirement}" for f in filters)


def _format_learning_examples(examples: list) -> str:
    """Render approved Bot Learning Log entries into a few-shot block.

    Returns "" if no examples, otherwise a multi-line string prefixed
    with LEARNING_EXAMPLES_PREAMBLE and terminated by a separator.
    Truncates each excerpt + note for prompt size safety."""
    if not examples:
        return ""
    parts = [LEARNING_EXAMPLES_PREAMBLE]
    for i, ex in enumerate(examples, 1):
        pos = (ex.get("position") or "").strip() or "(unspecified)"
        bot = (ex.get("bot_decision") or "").strip() or "(unknown)"
        hr = (ex.get("hr_outcome") or "").strip() or "(unknown)"
        notes = (ex.get("hr_notes") or "").strip()[:400]
        bot_reason = (ex.get("ai_reasoning") or "").strip()[:300]
        excerpt = (ex.get("resume_excerpt") or "").strip()[:600]
        parts.append(
            f"--- Correction {i} ---\n"
            f"Position applied for: {pos}\n"
            f"Bot's decision (wrong): {bot}\n"
            f"Bot's reasoning at the time: {bot_reason}\n"
            f"HR's final decision: {hr}\n"
            f"HR's note explaining why the bot was wrong: {notes}\n"
            f"Resume excerpt:\n{excerpt}\n"
        )
    parts.append("---\nApply the lesson above to the new resume below.\n\n")
    return "\n".join(parts)


def score(*, api_key: str, model: str, resume_text: str, filters: list,
          email_subject: str = "", email_body: str = "",
          used_ocr: bool = False,
          learning_examples: list | None = None) -> dict[str, Any]:
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

    learning_block = _format_learning_examples(learning_examples or [])
    if learning_block:
        user_msg = learning_block + user_msg

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
        "recruiter_agency": "N/A",
    }


def _coerce_level(raw: Any) -> str:
    if isinstance(raw, str):
        s = raw.strip().lower().replace("-", "_").replace(" ", "_")
        if s in _LEVEL_RANK:
            return s
        try:
            n = int(float(s))
            return _score_to_level(n)
        except ValueError:
            return "no_fit"
    if isinstance(raw, (int, float)):
        return _score_to_level(int(raw))
    return "no_fit"


def _score_to_level(n: int) -> str:
    if n >= 90:
        return "excellent"
    if n >= 70:
        return "strong"
    if n >= 50:
        return "borderline"
    if n >= 30:
        return "weak"
    return "no_fit"


_VALID_DECISIONS = {"qualified", "not_qualified", "needs_review", "not_a_resume"}
_VALID_HOPPING = {"positive", "caution", "neutral"}


def _normalize(r: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    decision = r.get("overall_decision")
    if decision not in _VALID_DECISIONS:
        decision = "needs_review"
    out["overall_decision"] = decision

    raw_roles = r.get("best_fit_roles") or []
    if not isinstance(raw_roles, list):
        raw_roles = []
    norm_roles = []
    for entry in raw_roles:
        if not isinstance(entry, dict):
            continue
        role_name = str(entry.get("role", "") or "").strip()
        if not role_name:
            continue
        fit = _coerce_level(entry.get("fit_level"))
        if fit == "no_fit":
            continue
        norm_roles.append({
            "role": role_name,
            "fit_level": fit,
            "reasoning": str(entry.get("reasoning", "") or "").strip(),
        })
    norm_roles.sort(key=lambda x: _LEVEL_RANK.get(x["fit_level"], 0), reverse=True)
    out["best_fit_roles"] = norm_roles

    yre = r.get("years_relevant_experience", 0)
    try:
        out["years_relevant_experience"] = int(float(yre))
    except (TypeError, ValueError):
        out["years_relevant_experience"] = 0

    hop = str(r.get("job_hopping_flag", "neutral") or "neutral").strip().lower()
    out["job_hopping_flag"] = hop if hop in _VALID_HOPPING else "neutral"

    out["reasoning"] = str(r.get("reasoning", "") or "").strip()

    try:
        c = float(r.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        c = 0.0
    out["confidence"] = max(0.0, min(1.0, c))

    out["candidate_name"] = str(r.get("candidate_name", "") or "").strip()
    out["candidate_email"] = str(r.get("candidate_email", "") or "").strip()
    out["candidate_phone"] = str(r.get("candidate_phone", "") or "").strip()

    applied = str(r.get("applied_for_role", "unspecified") or "unspecified").strip()
    if not applied:
        applied = "unspecified"
    out["applied_for_role"] = applied

    agency = str(r.get("recruiter_agency", "N/A") or "N/A").strip()
    if not agency or agency.lower() in {"n/a", "na", "none", "null", "unknown"}:
        agency = "N/A"
    out["recruiter_agency"] = agency

    return out
