"""
Overkill safety layer: verifies the scorer's not_qualified verdict is
actually consistent with its own reasoning before we fire an auto-denial.

Motivation: on 2026-07-20 Anthony Luna Moreno (row 383) got auto-denied
even though the AI reasoning literally said "excellent match for Cherry
Picker." The primary scorer's `decision` field and its `reasoning` field
had drifted out of sync -- the model wrote glowing text and then stamped
not_qualified without invoking the applied-for trump rule.

This module runs a second, independent Haiku call whose ONLY job is to
answer: "does this reasoning actually justify the not_qualified verdict?"
If not, we downgrade the row to needs_review and skip the denial email
entirely. Errors fail closed (treated as unsafe).

Called from main.py right after a bucket resolves to not_qualified,
before any outbound reply logic.
"""

from __future__ import annotations

import logging
import re

import anthropic


log = logging.getLogger(__name__)


# Haiku is cheap and fast; verification runs on every not_qualified.
_MODEL = "claude-haiku-4-5"

# Fail-closed timeout so a flaky API can't hold up the whole run.
_TIMEOUT_SECONDS = 20


# Regex fast-path: if any of these phrases appear in the reasoning, we
# ALWAYS run the LLM check even if the cheap heuristic below would have
# passed. These are the "positive endorsement" phrases that were the tell
# in the Anthony Luna case.
_POSITIVE_ENDORSEMENT_PATTERNS = [
    r"\bexcellent (candidate|match|fit|choice)\b",
    r"\bstrong (fit|match|candidate)\b",
    r"\bgreat (fit|match|candidate)\b",
    r"\bhighly (qualified|recommend)\b",
    r"\bqualifies (strongly|for)\b",
    r"\bmeets (the )?minimum\b",
    r"\bexceeds (the )?minimum\b",
    r"\bideal (candidate|fit)\b",
]

# Phrases that CORRECTLY justify not_qualified. If any appear alongside
# the endorsements, the reasoning may still be internally consistent
# (e.g. positive about cross-role fit but negative about applied-for
# role). The LLM check makes the final call in that case.
_JUSTIFIED_NEGATIVE_PATTERNS = [
    r"applied[- ]for trump rule",
    r"applied (role|position) is (weak|no_?fit)",
    r"does not (document|meet|show)",
    r"no (borderline|matching)",
    r"unrelated to (warehouse|material handling)",
    r"lacks the (stated|required|minimum)",
]


def _has_any(text: str, patterns: list[str]) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(re.search(p, lowered) for p in patterns)


_PROMPT_TEMPLATE = """You are a strict verification layer for a resume-scoring bot.

The primary scorer just classified a candidate as **not_qualified**. Your job: independently decide whether the scorer's own written reasoning actually justifies not_qualified, or whether the reasoning contradicts its verdict.

A "not_qualified" verdict is ONLY justified when the reasoning does at least ONE of these:
  (a) explicitly invokes the applied-for trump rule (e.g. "under the applied-for trump rule", "applied role is 'weak'", "does not document the stated minimum") with a concrete reason
  (b) explicitly states the candidate has NO borderline-or-better match across all available roles
  (c) explicitly states the resume is unrelated to warehouse / material-handling / forklift work
  (d) explicitly states the resume is unreadable, blank, or non-resume content

If the reasoning contains positive language like "excellent candidate", "strong fit", "great match", "qualifies strongly", "excellent match for [role]" and does NOT contain a subsequent explicit "however" clause invoking one of (a)-(d), the verdict is WRONG.

Return ONE word only: `safe` if the reasoning genuinely justifies not_qualified, or `unsafe` if the reasoning contradicts the verdict (meaning we should NOT fire the auto-denial).

---
Applied for role: {applied_for}
Bot confidence in not_qualified: {confidence}
Bot reasoning:
{reasoning}
---

Verdict (one word, `safe` or `unsafe`):"""


def verify_not_qualified(
    api_key: str,
    applied_for: str,
    confidence: float,
    reasoning: str,
) -> tuple[bool, str]:
    """Return (safe_to_deny, verdict_text).

    safe_to_deny == True means the reasoning genuinely supports the
    not_qualified verdict and it's safe to fire the auto-denial.

    safe_to_deny == False means the reasoning contradicts the verdict
    (e.g. calls the candidate excellent) OR the guard errored. In both
    cases the caller MUST downgrade the row to needs_review and skip
    the denial send. Fail-closed by design.

    The verdict_text is a short human-readable string suitable for
    logging and for stamping into Bot Feedback / Inbox Log so we can
    audit guard behavior later.
    """
    reasoning = (reasoning or "").strip()

    # Belt-and-suspenders cheap check: empty reasoning is by definition
    # not a justification. Do not spend an API call.
    if not reasoning:
        return False, "unsafe (empty reasoning)"

    # Cheap heuristic fast-fail: pure positive endorsement with no
    # negative justification phrase is a slam-dunk contradiction. Still
    # run the LLM to be safe (Ben asked for overkill), but log it.
    has_positive = _has_any(reasoning, _POSITIVE_ENDORSEMENT_PATTERNS)
    has_justified_negative = _has_any(reasoning, _JUSTIFIED_NEGATIVE_PATTERNS)

    prompt = _PROMPT_TEMPLATE.format(
        applied_for=applied_for or "(unspecified)",
        confidence=f"{confidence:.2f}",
        reasoning=reasoning,
    )

    try:
        client = anthropic.Anthropic(
            api_key=api_key,
            max_retries=3,
            timeout=_TIMEOUT_SECONDS,
        )
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=8,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = ""
        try:
            raw = (resp.content[0].text or "").strip().lower()
        except Exception:
            raw = ""

        verdict_safe = raw.startswith("safe")

        # If the heuristic screamed "contradiction" but the LLM disagreed,
        # we still fail closed. Ben's instruction: overkill on accuracy.
        if has_positive and not has_justified_negative and verdict_safe:
            log.warning(
                "Guard heuristic detected positive endorsement without "
                "justified-negative language, but LLM voted 'safe'. "
                "Overriding to unsafe (heuristic wins on conflict)."
            )
            return False, f"unsafe (heuristic-override; llm said {raw!r})"

        if verdict_safe:
            return True, f"safe (llm={raw!r})"
        return False, f"unsafe (llm={raw!r})"

    except anthropic.APIError as e:
        # Fail closed on any Anthropic error. Better to route to HR
        # review than to fire a wrong denial we can't take back.
        log.warning("Guard LLM call failed: %s. Failing closed (unsafe).", e)
        return False, f"unsafe (llm error: {type(e).__name__})"
    except Exception as e:  # noqa: BLE001
        log.warning("Guard unexpected error: %s. Failing closed.", e)
        return False, f"unsafe (unexpected error: {type(e).__name__})"
