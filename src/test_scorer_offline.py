"""Offline test scorer - no Google APIs needed. Reads filters from the
local filters.yaml, calls Claude, prints the result to stdout (which becomes
the GitHub Actions log).

Use this when you haven't set up GOOGLE_OAUTH_REFRESH_TOKEN yet. Once that
secret exists, prefer the regular test-scorer.yml workflow which writes
results to a 'Test Results' tab in the Sheet."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sys

import yaml

from . import scorer, sheets_client


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("test-scorer-offline")


def _load_yaml_filters() -> list[sheets_client.Filter]:
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    yaml_path = repo_root / "filters.yaml"
    if not yaml_path.exists():
        log.error("filters.yaml not found at %s", yaml_path)
        return []
    data = yaml.safe_load(yaml_path.read_text())
    out = []
    for f in (data.get("filters") or []):
        out.append(sheets_client.Filter(
            role=f["role"],
            requirement=f["requirement"],
            job_hopping=f.get("job_hopping", ""),
            active=f.get("active", True),
        ))
    return out


def run() -> int:
    resume_text = os.environ.get("RESUME_TEXT", "").strip()
    if not resume_text:
        log.error("RESUME_TEXT is empty - paste resume text in the workflow input.")
        return 2

    email_subject = os.environ.get("EMAIL_SUBJECT", "")
    email_body = os.environ.get("EMAIL_BODY", "")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    if not api_key:
        log.error("ANTHROPIC_API_KEY is missing.")
        return 2

    filters = _load_yaml_filters()
    if not filters:
        log.error("No filters in filters.yaml - cannot score.")
        return 3
    log.info("Loaded %d filter(s) from filters.yaml.", len(filters))
    for f in filters:
        log.info("  - %s%s", f.role, "" if f.active else "  (PAUSED)")

    log.info("Calling Claude...")
    result = scorer.score(
        api_key=api_key,
        model=model,
        resume_text=resume_text,
        filters=filters,
        email_subject=email_subject,
        email_body=email_body,
    )

    # Pretty-print the full result so it's readable in the Actions log.
    print("\n" + "=" * 70)
    print("SCORING RESULT")
    print("=" * 70)
    print(json.dumps(result, indent=2))
    print("=" * 70)
    print(f"\nDecision: {result['overall_decision']}")
    print(f"Confidence: {result['confidence']}")
    print(f"Years relevant exp: {result['years_relevant_experience']}")
    print(f"Job hopping: {result['job_hopping_flag']}")
    print("\nBest-fit roles (sorted high to low):")
    for r in result["best_fit_roles"]:
        print(f"  {r['fit_level']:>10}  {r['role']}")
        if r.get("reasoning"):
            print(f"        \\ {r['reasoning']}")
    print(f"\nOverall reasoning:\n{result['reasoning']}")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(run())
