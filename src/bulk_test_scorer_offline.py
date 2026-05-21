"""Bulk offline test scorer. Reads tests/test-resumes.yaml, scores every
test against the active filters in filters.yaml, prints a summary table
to the Actions log.

No Google APIs touched - only Anthropic. Useful for calibrating filter
wording and the scoring rubric before the bot goes live."""

from __future__ import annotations

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
log = logging.getLogger("bulk-test")


def _load_filters() -> list[sheets_client.Filter]:
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    yaml_path = repo_root / "filters.yaml"
    if not yaml_path.exists():
        return []
    data = yaml.safe_load(yaml_path.read_text())
    out: list[sheets_client.Filter] = []
    for f in (data.get("filters") or []):
        out.append(sheets_client.Filter(
            role=f["role"],
            requirement=f["requirement"],
            job_hopping=f.get("job_hopping", ""),
            active=f.get("active", True),
        ))
    return out


def _load_tests() -> list[dict]:
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    yaml_path = repo_root / "tests" / "test-resumes.yaml"
    if not yaml_path.exists():
        log.error("tests/test-resumes.yaml not found at %s", yaml_path)
        return []
    data = yaml.safe_load(yaml_path.read_text())
    return data.get("tests") or []


def run() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY missing.")
        return 2
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    filters = _load_filters()
    if not filters:
        log.error("No filters in filters.yaml.")
        return 3

    tests = _load_tests()
    if not tests:
        log.error("No tests in tests/test-resumes.yaml.")
        return 4

    log.info("Running %d test(s) against %d filter(s).", len(tests), len(filters))
    print()

    summary: list[tuple[str, str, str, str, float, str, str]] = []
    # tuple: (name, expected, got, pass_fail, conf, applied_for, top_role)

    for i, t in enumerate(tests, 1):
        name = t.get("name") or f"test {i}"
        expected = t.get("expected_decision", "?")
        subject = t.get("email_subject", "") or ""
        body = t.get("email_body", "") or ""
        resume = t.get("resume", "") or ""

        print("=" * 70)
        print(f"Test {i}: {name}")
        print(f"Expected decision: {expected}")
        if subject:
            print(f"Email subject:     {subject}")
        if body:
            print(f"Email body:        {body[:120]}{'...' if len(body) > 120 else ''}")
        print("-" * 70)

        result = scorer.score(
            api_key=api_key,
            model=model,
            resume_text=resume,
            filters=filters,
            email_subject=subject,
            email_body=body,
        )

        decision = result["overall_decision"]
        conf = float(result.get("confidence") or 0)
        applied = result.get("applied_for_role", "unspecified") or "unspecified"
        roles_text = ", ".join(
            f"{r['role']} ({r['fit_score']})" for r in result["best_fit_roles"]
        )
        pass_fail = "PASS" if decision == expected else "FAIL"
        top_role = (
            result["best_fit_roles"][0]["role"]
            if result["best_fit_roles"] else ""
        )

        print(f"Got decision: {decision}  [{pass_fail}]")
        print(f"Confidence:   {conf:.2f}")
        print(f"Applied for:  {applied}")
        print(f"Best-fit:     {roles_text or '(none)'}")
        print(f"AI reasoning: {result['reasoning']}")
        print()

        summary.append((name, expected, decision, pass_fail, conf, applied, top_role))

    # Final summary table
    print()
    print("=" * 88)
    print("BULK TEST SUMMARY")
    print("=" * 88)
    print(f"{'#':<3}{'PF':<6}{'Expected':<16}{'Got':<16}{'Conf':<7}{'Applied':<28}{'Test'}")
    print("-" * 88)
    for i, (name, exp, got, pf, conf, applied, top) in enumerate(summary, 1):
        short_applied = (applied[:25] + "...") if len(applied) > 28 else applied
        print(f"{i:<3}{pf:<6}{exp:<16}{got:<16}{conf:<7.2f}{short_applied:<28}{name}")
    print("-" * 88)

    passed = sum(1 for r in summary if r[3] == "PASS")
    total = len(summary)
    print(f"Passed: {passed} / {total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
