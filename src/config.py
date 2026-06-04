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


def _optional_int(key: str, default: int) -> int:
    val = (_optional(key, "") or "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _optional_float(key: str, default: float) -> float:
    val = (_optional(key, "") or "").strip()
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _optional_bool(key: str, default: bool = False) -> bool:
    val = (_optional(key, "") or "").strip().lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "on"}


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
    folder_internal: str

    sheet_id: str
    filters_tab: str
    dashboard_tab: str
    templates_tab: str
    errors_tab: str
    misc_tab: str
    inbox_log_tab: str
    needs_human_tab: str

    processed_label: str
    max_messages_per_run: int
    company_name: str

    shadow_mode: bool

    bot_start_date: str

    internal_domains: tuple

    # ----- Pre-filter blocklist -----
    # Sender emails / domains to always classify as MISC without an LLM call.
    # Comma-separated. Domain entries can be "example.com" or "@example.com".
    blocklist_senders: tuple

    # ----- Needs Human review queue -----
    # If the classifier's confidence is below this, route the email to
    # the Needs Human queue (Gmail label + sheet tab) instead of acting.
    classifier_confidence_threshold: float
    # If the same sender has N+ messages in the past LOOP_WINDOW_HOURS,
    # route to Needs Human as a loop-detection signal.
    loop_threshold: int
    loop_window_hours: int

    # ----- Business-hours-only auto-replies -----
    # When true, template auto-replies fire only inside the PT window
    # [business_hours_start_pt, business_hours_end_pt). Outside the
    # window the bot still classifies, scores, and logs -- it just
    # skips the actual outbound send and does NOT mark Gmail-processed,
    # so the next business-hours run picks the email back up.
    business_hours_only_replies: bool
    business_hours_start_pt: int
    business_hours_end_pt: int


def load() -> Config:
    internal_domains_raw = _optional("INTERNAL_DOMAINS", "r1concepts.com")
    internal_domains = tuple(
        d.strip().lower().lstrip("@") for d in internal_domains_raw.split(",")
        if d.strip()
    )
    blocklist_raw = _optional("BLOCKLIST_SENDERS", "")
    blocklist = tuple(
        s.strip().lower() for s in blocklist_raw.split(",") if s.strip()
    )
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
        folder_internal=_optional("DRIVE_FOLDER_INTERNAL", ""),
        sheet_id=_required("SHEET_ID"),
        filters_tab=_optional("FILTERS_TAB_NAME", "Filters"),
        dashboard_tab=_optional("DASHBOARD_TAB_NAME", "Candidates"),
        templates_tab=_optional("TEMPLATES_TAB_NAME", "Templates"),
        errors_tab=_optional("ERRORS_TAB_NAME", "Bot Errors"),
        misc_tab=_optional("MISC_TAB_NAME", "Archive - Misc"),
        inbox_log_tab=_optional("INBOX_LOG_TAB_NAME", "Inbox Log"),
        needs_human_tab=_optional("NEEDS_HUMAN_TAB_NAME", "Needs Human"),
        processed_label=_optional("PROCESSED_LABEL", "resume-bot/processed"),
        max_messages_per_run=int(_optional("MAX_MESSAGES_PER_RUN", "25")),
        company_name=_optional("COMPANY_NAME", "R1 Concepts"),
        shadow_mode=_optional_bool("SHADOW_MODE", False),
        bot_start_date=_optional("BOT_START_DATE", "").strip(),
        internal_domains=internal_domains,
        blocklist_senders=blocklist,
        classifier_confidence_threshold=_optional_float(
            "CLASSIFIER_CONFIDENCE_THRESHOLD", 0.7,
        ),
        loop_threshold=_optional_int("LOOP_THRESHOLD", 3),
        loop_window_hours=_optional_int("LOOP_WINDOW_HOURS", 24),
        business_hours_only_replies=_optional_bool(
            "BUSINESS_HOURS_ONLY_REPLIES", True,
        ),
        business_hours_start_pt=_optional_int("BUSINESS_HOURS_START_PT", 8),
        business_hours_end_pt=_optional_int("BUSINESS_HOURS_END_PT", 19),
    )
