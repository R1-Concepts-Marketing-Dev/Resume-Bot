"""Builds Google API service clients from an OAuth refresh token.

A one-time browser authorization (done via Google's OAuth Playground) signs in
as jobs@r1concepts.com and produces a long-lived refresh token. That token,
along with the OAuth client ID and secret, are stored as GitHub Secrets. On
each bot run we exchange the refresh token for a short-lived access token and
use it to call Gmail / Drive / Sheets."""

from __future__ import annotations

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# Scopes the user (jobs@) granted when they signed in via OAuth Playground.
# These must match what was requested during that one-time authorization.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
]


def make_credentials(client_id: str, client_secret: str, refresh_token: str) -> Credentials:
    """Build a Credentials object and refresh it once to get an access token."""
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    # Forces an immediate refresh — fails fast if creds are invalid.
    creds.refresh(Request())
    return creds


def gmail(creds: Credentials):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def drive(creds: Credentials):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def sheets(creds: Credentials):
    return build("sheets", "v4", credentials=creds, cache_discovery=False)
