"""Builds Google API service clients from a service account with domain-wide
delegation. The service account impersonates a real Workspace user (the jobs@
inbox) so it can read Gmail, write to Drive, and write to Sheets on that
user's behalf."""

from __future__ import annotations

from google.oauth2 import service_account
from googleapiclient.discovery import build

# Scopes the service account needs. These must match what's authorized in the
# Workspace admin console under Security → API controls → Domain-wide delegation.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
]


def _credentials(service_account_info: dict, subject: str):
    creds = service_account.Credentials.from_service_account_info(
        service_account_info, scopes=SCOPES
    )
    # Impersonate the inbox user (e.g. jobs@r1concepts.com) via DWD.
    return creds.with_subject(subject)


def gmail(service_account_info: dict, subject: str):
    return build(
        "gmail", "v1",
        credentials=_credentials(service_account_info, subject),
        cache_discovery=False,
    )


def drive(service_account_info: dict, subject: str):
    return build(
        "drive", "v3",
        credentials=_credentials(service_account_info, subject),
        cache_discovery=False,
    )


def sheets(service_account_info: dict, subject: str):
    return build(
        "sheets", "v4",
        credentials=_credentials(service_account_info, subject),
        cache_discovery=False,
    )
