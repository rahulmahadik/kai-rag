"""Stored Teams conversation references for proactive DM.

Bot Framework cannot cold-open a 1:1 chat from only a user id the way Slack can.
After a user messages the bot we persist enough of the Activity to POST a later
proactive message to the same conversation (typically a personal DM if they
DMed the bot once). Used by ``POST /notify`` when ``CHAT_PLATFORM=teams``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from kai.config import Settings

logger = logging.getLogger("kai.chat.teams_refs")

_CONNECTOR_SCOPE = "https://api.botframework.com/.default"


@dataclass(frozen=True)
class TeamsConversationRef:
    user_key: str
    service_url: str
    conversation_id: str
    bot_id: str
    user_id: str
    tenant_id: str = ""


def conversation_ref_from_activity(activity: dict) -> TeamsConversationRef | None:
    """Extract a storable ref from an inbound Activity, or None if incomplete."""

    frm = activity.get("from") or {}
    recipient = activity.get("recipient") or {}  # the bot
    conversation = activity.get("conversation") or {}
    service_url = (activity.get("serviceUrl") or "").rstrip("/")
    user_key = (frm.get("aadObjectId") or frm.get("id") or "").strip()
    user_id = (frm.get("id") or user_key).strip()
    bot_id = (recipient.get("id") or "").strip()
    conv_id = (conversation.get("id") or "").strip()
    tenant = (conversation.get("tenantId") or "").strip()
    if not (user_key and service_url and conv_id and bot_id and user_id):
        return None
    return TeamsConversationRef(
        user_key=user_key,
        service_url=service_url,
        conversation_id=conv_id,
        bot_id=bot_id,
        user_id=user_id,
        tenant_id=tenant,
    )


class TeamsConversationStore:
    """Postgres-backed map of Teams user → last conversation reference."""

    def __init__(self, database_url: str) -> None:
        self._db = (database_url or "").strip()
        self._ready = False

    def _connect(self):  # noqa: ANN202
        import psycopg

        return psycopg.connect(self._db)

    def _ensure(self, conn) -> None:  # noqa: ANN001
        if self._ready:
            return
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS kai_teams_conversations (
                    user_key         text PRIMARY KEY,
                    service_url      text NOT NULL,
                    conversation_id  text NOT NULL,
                    bot_id           text NOT NULL,
                    user_id          text NOT NULL,
                    tenant_id        text NOT NULL DEFAULT '',
                    updated_at       timestamptz NOT NULL DEFAULT now()
                )
                """
            )
        conn.commit()
        self._ready = True

    def remember(self, activity: dict) -> bool:
        """Upsert a ref from ``activity``. Returns True when something was stored."""

        if not self._db:
            return False
        ref = conversation_ref_from_activity(activity)
        if ref is None:
            return False
        with self._connect() as conn:
            self._ensure(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO kai_teams_conversations
                        (user_key, service_url, conversation_id, bot_id, user_id, tenant_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_key) DO UPDATE SET
                        service_url = EXCLUDED.service_url,
                        conversation_id = EXCLUDED.conversation_id,
                        bot_id = EXCLUDED.bot_id,
                        user_id = EXCLUDED.user_id,
                        tenant_id = EXCLUDED.tenant_id,
                        updated_at = now()
                    """,
                    (
                        ref.user_key,
                        ref.service_url,
                        ref.conversation_id,
                        ref.bot_id,
                        ref.user_id,
                        ref.tenant_id,
                    ),
                )
            conn.commit()
        return True

    def get(self, user_key: str) -> TeamsConversationRef | None:
        key = (user_key or "").strip()
        if not key or not self._db:
            return None
        with self._connect() as conn:
            self._ensure(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_key, service_url, conversation_id, bot_id, user_id, tenant_id "
                    "FROM kai_teams_conversations WHERE user_key = %s",
                    (key,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return TeamsConversationRef(*row)


def _connector_token(settings: Settings) -> str:
    app_id = (getattr(settings, "teams_app_id", "") or "").strip()
    password = (getattr(settings, "teams_app_password", "") or "").strip()
    tenant = (getattr(settings, "teams_app_tenant_id", "") or "").strip() or "botframework.com"
    if not app_id or not password:
        raise ValueError("TEAMS_APP_ID and TEAMS_APP_PASSWORD are not configured")
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    r = httpx.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": app_id,
            "client_secret": password,
            "scope": _CONNECTOR_SCOPE,
        },
        timeout=20.0,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def send_teams_proactive(settings: Settings, recipient: str, message: str) -> tuple[bool, str]:
    """DM ``recipient`` using a previously stored conversation reference."""

    db = (getattr(settings, "database_url", "") or "").strip()
    if not db:
        return False, "DATABASE_URL is not configured"
    if (
        not (getattr(settings, "teams_app_id", "") or "").strip()
        or not (getattr(settings, "teams_app_password", "") or "").strip()
    ):
        return False, "TEAMS_APP_ID and TEAMS_APP_PASSWORD are not configured"

    store = TeamsConversationStore(db)
    ref = store.get(recipient)
    if ref is None:
        return False, (
            "No stored Teams conversation for that user "
            "(they must message the bot once first, ideally in a 1:1 chat)"
        )

    conversation: dict = {"id": ref.conversation_id}
    if ref.tenant_id:
        conversation["tenantId"] = ref.tenant_id
    activity = {
        "type": "message",
        "from": {"id": ref.bot_id},
        "conversation": conversation,
        "recipient": {"id": ref.user_id},
        "textFormat": "markdown",
        "text": message,
    }
    url = f"{ref.service_url}/v3/conversations/{ref.conversation_id}/activities"
    try:
        token = _connector_token(settings)
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=activity,
            timeout=30.0,
        )
        if resp.status_code >= 400:
            logger.warning(
                "kai_teams_notify_failed status=%d body=%s",
                resp.status_code,
                (resp.text or "")[:200],
            )
            return False, f"Teams Connector error: HTTP {resp.status_code}"
        return True, "sent"
    except ValueError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kai_teams_notify_failed err=%s", type(exc).__name__)
        return False, type(exc).__name__
