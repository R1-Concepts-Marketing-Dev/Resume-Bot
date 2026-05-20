"""Environment-driven configuration. Reads from process env (set by GitHub
Actions secrets in production, .env file locally)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass


def _required(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val


def _optional(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Config:
    # Google auth
    service_account_info: dict
    gmail_user: str

    # Anthropic
    anthropic_api_key: str
    anthropic_model: str

    # Google Drive folder IDs
    folder_qualified: str
    folder_not_qualified: str
    folder_review: str
    folder_incoming: str  # optional, "" if not set

    # Google Sheet
    sheet_id: str
    filters_tab: str
    dashboard_tab: str

    # Behaviour
    processed_label: str
    max_messages_per_run: int


def load() -> Config:
    sa_raw = _required("GOOGLE_SA_JSON")
    try:
        sa_info = json.loads(sa_raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"GOOGLE_SA_JSON is not valid JSON: {e}") from e

    return Config(
        service_account_info=sa_info,
        gmail_user=_required("GMAIL_USER"),
        anthropic_api_key=_required("ANTHROPIC_API_KEY"),
        anthropic_model=_optional("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        folder_qualified=_required("DRIVE_FOLDER_QUALIFIED"),
        folder_not_qualified=_required("DRIVE_FOLDER_NOT_QUALIFIED"),
        folder_review=_required("DRIVE_FOLDER_REVIEW"),
        folder_incoming=_optional("DRIVE_FOLDER_INCOMING", ""),
        sheet_id=_required("SHEET_ID"),
        filters_tab=_optional("FILTERS_TAB_NAME", "Filters"),
        dashboard_tab=_optional("DASHBOARD_TAB_NAME", "Candidates"),
        processed_label=_optional("PROCESSED_LABEL", "resume-bot/processed"),
        max_messages_per_run=int(_optional("MAX_MESSAGES_PER_RUN", "25")),
    )
