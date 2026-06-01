"""Environment-driven configuration."""

from __future__ import annotations

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
    oauth_client_id: str
    oauth_client_secret: str
    oauth_refresh_token: str
    gmail_user: str

    anthropic_api_key: str
    anthropic_model: str

    folder_qualified: str
    folder_not_qualified: str
    folder_review: str
    folder_pending: str
    folder_incoming: str

    sheet_id: str
    filters_tab: str
    dashboard_tab: str
    templates_tab: str
    errors_tab: str
    misc_tab: str

    processed_label: str
    max_messages_per_run: int
    company_name: str

    shadow_mode: bool

    # Floor date for inbox lookup. Empty = no floor.
    bot_start_date: str


def load() -> Config:
    return Config(
        oauth_client_id=_required("GOOGLE_OAUTH_CLIENT_ID"),
        oauth_client_secret=_required("GOOGLE_OAUTH_CLIENT_SECRET"),
        oauth_refresh_token=_required("GOOGLE_OAUTH_REFRESH_TOKEN"),
        gmail_user=_required("GMAIL_USER"),
        anthropic_api_key=_required("ANTHROPIC_API_KEY"),
        anthropic_model=_optional("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        folder_qualified=_required("DRIVE_FOLDER_QUALIFIED"),
        folder_not_qualified=_required("DRIVE_FOLDER_NOT_QUALIFIED"),
        folder_review=_required("DRIVE_FOLDER_REVIEW"),
        folder_pending=_required("DRIVE_FOLDER_PENDING"),
        folder_incoming=_optional("DRIVE_FOLDER_INCOMING", ""),
        sheet_id=_required("SHEET_ID"),
        filters_tab=_optional("FILTERS_TAB_NAME", "Filters"),
        dashboard_tab=_optional("DASHBOARD_TAB_NAME", "Candidates"),
        templates_tab=_optional("TEMPLATES_TAB_NAME", "Templates"),
        errors_tab=_optional("ERRORS_TAB_NAME", "Bot Errors"),
        misc_tab=_optional("MISC_TAB_NAME", "Archive - Misc"),
        processed_label=_optional("PROCESSED_LABEL", "resume-bot/processed"),
        max_messages_per_run=int(_optional("MAX_MESSAGES_PER_RUN", "25")),
        company_name=_optional("COMPANY_NAME", "R1 Concepts"),
        shadow_mode=_optional("SHADOW_MODE", "false").strip().lower()
                    in {"1", "true", "yes", "on"},
        bot_start_date=_optional("BOT_START_DATE", "").strip(),
    )
