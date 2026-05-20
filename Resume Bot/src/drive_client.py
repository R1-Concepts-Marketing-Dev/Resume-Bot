"""Drive operations: upload a resume file into a destination folder, return
a shareable webViewLink for the dashboard."""

from __future__ import annotations

import io

from googleapiclient.http import MediaIoBaseUpload


def upload(svc, filename: str, data: bytes, mime_type: str, folder_id: str) -> str:
    """Uploads bytes to Drive under folder_id. Returns the file's webViewLink."""
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
    created = svc.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()
    return created.get("webViewLink", "")
