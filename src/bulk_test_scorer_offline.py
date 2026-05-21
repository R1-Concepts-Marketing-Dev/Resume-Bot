"""Bulk offline test scorer. Reads tests/test-resumes.yaml, scores every
test against the active filters in filters.yaml, prints a summary table
to the Actions log. No Google APIs touched."""

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
    # Accept either {"tests": [...]} or a bare list at the top level,
    # since hand-editing the YAML can flatten the wrapper accidentally.
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("tests") or []
    return []


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

    summary: list[tuple[str, str, str, str, float, str]] = []

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
            preview = body[:120] + ("..." if len(body) > 120 else "")
            print(f"Email body:        {preview}")
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

        print(f"Got decision: {decision}  [{pass_fail}]")
        print(f"Confidence:   {conf:.2f}")
        print(f"Applied for:  {applied}")
        print(f"Best-fit:     {roles_text or '(none)'}")
        print(f"AI reasoning: {result['reasoning']}")
        print()

        summary.append((name, expected, decision, pass_fail, conf, applied))

    print()
    print("=" * 88)
    print("BULK TEST SUMMARY")
    print("=" * 88)
    header = f"{'#':<3}{'PF':<6}{'Expected':<16}{'Got':<16}{'Conf':<7}{'Applied':<28}{'Test'}"
    print(header)
    print("-" * 88)
    for i, (name, exp, got, pf, conf, applied) in enumerate(summary, 1):
        short_app = applied[:25] + "..." if len(applied) > 28 else applied
        row = f"{i:<3}{pf:<6}{exp:<16}{got:<16}{conf:<7.2f}{short_app:<28}{name}"
        print(row)
    print("-" * 88)

    passed = sum(1 for r in summary if r[3] == "PASS")
    total = len(summary)
    print(f"Passed: {passed} / {total}")
    # Always return 0 - this is an exploratory tool, not a CI gate. A FAIL
    # row means the AI disagreed with my expected_decision, not that the
    # bot broke. Returning 1 caused Actions to show the run as a red X
    # which is misleading. Read the per-test reasoning to decide whether
    # to tune filters or update expectations.
    return 0


if __name__ == "__main__":
    sys.exit(run())
