"""Download helpers for chat-platform file attachments (Slack / Teams)."""

from __future__ import annotations

import logging
from urllib.parse import unquote, urlparse

import httpx

logger = logging.getLogger("kai.chat.files")


def download_url(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    max_bytes: int = 0,
    filename_hint: str = "",
) -> tuple[str, bytes] | None:
    """GET ``url`` and return ``(filename, bytes)``, or None on failure/oversize."""

    if not url:
        return None
    try:
        with httpx.stream(
            "GET", url, headers=headers or {}, timeout=60.0, follow_redirects=True
        ) as resp:
            if resp.status_code != 200:
                logger.warning("kai_file_download_status status=%d", resp.status_code)
                return None
            cl = resp.headers.get("content-length")
            if max_bytes > 0 and cl and int(cl) > max_bytes:
                logger.warning("kai_file_download_too_large content_length=%s", cl)
                return None
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if max_bytes > 0 and len(buf) > max_bytes:
                    logger.warning("kai_file_download_too_large bytes=%d", len(buf))
                    return None
            data = bytes(buf)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kai_file_download_failed err=%s", type(exc).__name__)
        return None

    name = filename_hint or _filename_from_url(url) or "attachment"
    return name, data


def _filename_from_url(url: str) -> str:
    path = unquote(urlparse(url).path or "")
    return path.rsplit("/", 1)[-1] if path else ""


def slack_file_download(
    bot_token: str, file_obj: dict, *, max_bytes: int = 0
) -> tuple[str, bytes] | None:
    """Download a Slack file object using the bot token on ``url_private``."""

    url = (file_obj.get("url_private_download") or file_obj.get("url_private") or "").strip()
    name = (file_obj.get("name") or file_obj.get("title") or "attachment").strip()
    return download_url(
        url,
        headers={"Authorization": f"Bearer {bot_token}"},
        max_bytes=max_bytes,
        filename_hint=name,
    )


def teams_attachment_download(
    attachment: dict, *, max_bytes: int = 0, auth_header: str = ""
) -> tuple[str, bytes] | None:
    """Download a Teams activity attachment via ``contentUrl`` when present."""

    url = (attachment.get("contentUrl") or "").strip()
    name = (attachment.get("name") or "attachment").strip()
    headers = {"Authorization": auth_header} if auth_header else None
    return download_url(url, headers=headers, max_bytes=max_bytes, filename_hint=name)
