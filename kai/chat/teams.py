"""Microsoft Teams adapter, inbound Bot Framework webhook.

Unlike Webex/Slack (outbound websocket, no public URL), Teams is **inbound**: Azure
Bot Service POSTs each Activity to an HTTPS endpoint you host. This adapter serves
that endpoint (`POST /api/messages`), routes every message through the SAME
:class:`~kai.chat.service.ChatService` as the other platforms, and replies via the
Bot Connector API. The 👍/👎 Adaptive Card is reused verbatim from Webex.

REQUIRES (see doc/integrations-setup.md): an Azure Bot registration
(`TEAMS_APP_ID` + `TEAMS_APP_PASSWORD`), a **public HTTPS URL** pointing at
`/api/messages`, and the extra: `pip install '.[teams]'`.

⚠️ Shipped but NOT integration-tested from this repo. It needs a live Azure tenant +
public endpoint to exercise. The pure parsing/routing helpers (`parse_activity`,
`_strip_mention`, `build_reply_activity`) ARE unit-tested; the Connector + auth
round-trip must be verified in your tenant.
"""

from __future__ import annotations

import logging
import re
import threading
import time

from kai.chat.base import FeedbackEvent, IncomingMessage
from kai.chat.service import HELP_TEXT, ChatService, format_reply, is_help_request, split_message
from kai.chat.webex import feedback_card  # the Adaptive Card JSON is portable to Teams

logger = logging.getLogger("kai.teams")

_MENTION_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.DOTALL)
_CONNECTOR_SCOPE = "https://api.botframework.com/.default"
_BF_JWKS = "https://login.botframework.com/v1/.well-known/keys"
_BF_ISSUER = "https://api.botframework.com"
_TEAMS_TEXT_LIMIT = 25_000  # headroom under Teams' ~28KB message cap (split long replies)


# --------------------------------------------------------------------------- #
# Pure helpers (no network), unit-tested
# --------------------------------------------------------------------------- #
def _strip_mention(text: str) -> str:
    """Remove the bot @mention (Teams wraps it as ``<at>Name</at>``) and trim."""

    return _MENTION_RE.sub("", text or "").strip()


def parse_activity(activity: dict) -> tuple[str, IncomingMessage | None, FeedbackEvent | None]:
    """Classify an inbound Teams Activity.

    Returns ``(kind, message, feedback)`` where kind is ``"message"`` (a question →
    /ask), ``"feedback"`` (an Adaptive-Card 👍/👎/escalate tap → /feedback|/escalate),
    or ``"ignore"`` (anything else). Pure, safe to unit-test without Azure.
    """

    if activity.get("type") != "message":
        return "ignore", None, None
    frm = activity.get("from") or {}
    sender = frm.get("aadObjectId") or frm.get("id") or ""

    # An Adaptive-Card Action.Submit arrives as a message whose `value` is the data.
    value = activity.get("value")
    if isinstance(value, dict) and value.get("callback_keyword") == "kai_feedback":
        return (
            "feedback",
            None,
            FeedbackEvent(
                verdict=value.get("verdict", ""),
                question=value.get("question", ""),
                sender_email=sender,
                raw=activity,
            ),
        )

    text = _strip_mention(activity.get("text", ""))
    if not text:
        return "ignore", None, None
    return "message", IncomingMessage(text=text, sender_email=sender, raw=activity), None


def build_reply_activity(incoming: dict, text: str, card: dict | None = None) -> dict:
    """Build the reply Activity to POST back to the Connector. Pure."""

    reply: dict = {
        "type": "message",
        "from": incoming.get("recipient"),  # the bot was the recipient of the inbound msg
        "conversation": incoming.get("conversation"),
        "recipient": incoming.get("from"),
        "replyToId": incoming.get("id"),
        "textFormat": "markdown",
        "text": text,
    }
    if card:
        reply["attachments"] = [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
        ]
    return reply


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class TeamsAdapter:
    """Serves the Bot Framework webhook and bridges Teams ↔ KAI's ChatService."""

    name = "teams"

    def __init__(self, settings) -> None:  # noqa: ANN001 - Settings, avoid import cycle
        self._service = ChatService(settings)
        self._app_id = (getattr(settings, "teams_app_id", "") or "").strip()
        self._app_password = (getattr(settings, "teams_app_password", "") or "").strip()
        self._tenant = (
            getattr(settings, "teams_app_tenant_id", "") or ""
        ).strip() or "botframework.com"
        self._host = getattr(settings, "teams_host", "0.0.0.0")
        self._port = int(getattr(settings, "teams_port", 3978) or 3978)
        self._show_card = bool(getattr(settings, "webex_feedback_card", False))
        self._jwks = None  # lazily-built PyJWKClient
        self._token: str | None = None  # cached Connector token (+ expiry)
        self._token_exp: float = 0.0
        self._token_lock = threading.Lock()

    # ---- Bot Framework auth (inbound) -------------------------------------- #
    def _validate(self, authorization: str, expected_service_url: str = "") -> None:
        """Validate the inbound Bot Framework JWT, or REFUSE when unconfigured.

        With no ``TEAMS_APP_ID`` we cannot verify the caller, so we reject rather than
        accept unauthenticated activities: an open webhook could be driven to reply
        (and leak a connector token to an attacker-supplied ``serviceUrl``). When
        ``expected_service_url`` is given we also bind it to the token's ``serviceurl``
        claim, so a valid token can't redirect our connector reply to another host."""

        if not self._app_id:
            raise RuntimeError(
                "Teams is not configured (TEAMS_APP_ID unset), refusing inbound requests. "
                "Set TEAMS_APP_ID / TEAMS_APP_PASSWORD to run the Teams bot."
            )
        try:
            import jwt  # from the [teams] extra (pyjwt[crypto])
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Teams needs the [teams] extra: pip install '.[teams]'") from exc
        token = (authorization or "").removeprefix("Bearer ").strip()
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(_BF_JWKS)
        key = self._jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=self._app_id, issuer=_BF_ISSUER
        )
        # Bind the reply target to the token: when we have an activity serviceUrl, the
        # token MUST carry a matching serviceurl claim. A missing/empty/mismatched claim
        # is a hard failure, otherwise a signed token with no claim could redirect our
        # connector reply (and its bearer token) to an attacker-supplied host.
        claim_url = (claims.get("serviceurl") or "").rstrip("/")
        if expected_service_url and claim_url.lower() != expected_service_url.rstrip("/").lower():
            raise RuntimeError("activity serviceUrl does not match the token's serviceurl claim")

    # ---- Connector API (outbound reply) ------------------------------------ #
    def _connector_token(self) -> str:
        import httpx

        # Serve a still-valid cached token under the lock; do the network fetch OUTSIDE
        # the lock so concurrent replies aren't serialized behind one token request
        # (worst case two threads refresh at once, harmless, last write wins).
        with self._token_lock:
            if self._token and time.monotonic() < self._token_exp:
                return self._token  # reuse until ~60s before expiry
        url = f"https://login.microsoftonline.com/{self._tenant}/oauth2/v2.0/token"
        r = httpx.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._app_id,
                "client_secret": self._app_password,
                "scope": _CONNECTOR_SCOPE,
            },
            timeout=20.0,
        )
        r.raise_for_status()
        body = r.json()
        token = body["access_token"]
        with self._token_lock:
            self._token = token
            self._token_exp = time.monotonic() + max(0, int(body.get("expires_in", 3600)) - 60)
        return token

    def _reply(self, activity: dict, text: str, card: dict | None) -> None:
        import httpx

        service_url = (activity.get("serviceUrl") or "").rstrip("/")
        conv = (activity.get("conversation") or {}).get("id")
        if not service_url or not conv:
            logger.warning("kai_teams_reply_skipped missing serviceUrl/conversation")
            return
        pieces = split_message(text, _TEAMS_TEXT_LIMIT) or [text]
        url = f"{service_url}/v3/conversations/{conv}/activities"
        try:
            token = self._connector_token()
        except Exception as exc:  # noqa: BLE001 - token fetch failed; nothing sent
            logger.error("kai_teams_token_failed err=%s: %s", type(exc).__name__, exc)
            return

        def _send(activity_json: dict) -> bool:
            try:
                httpx.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json=activity_json,
                    timeout=30.0,
                ).raise_for_status()
                return True
            except Exception as exc:  # noqa: BLE001 - log, never crash the webhook
                logger.error("kai_teams_reply_failed err=%s: %s", type(exc).__name__, exc)
                return False

        for i, piece in enumerate(pieces):
            # the feedback card rides only on the LAST piece (mirrors Webex/Slack)
            ok = _send(
                build_reply_activity(activity, piece, card if i == len(pieces) - 1 else None)
            )
            if not ok:
                # A mid-stream failure would leave a SILENT truncated answer, tell the
                # user the reply was cut off (best-effort) rather than look complete.
                if i > 0:
                    _send(
                        build_reply_activity(
                            activity, "_(My reply was cut off, please ask again.)_", None
                        )
                    )
                return

    def _retire_card(self, activity: dict, text: str) -> None:
        """Replace the tapped feedback card with static text so it can't be re-submitted.

        Teams' Action.Submit buttons stay clickable after a tap; without this a user
        could re-tap and file duplicate escalations / repeated downvotes. The inbound
        feedback activity's ``replyToId`` is the card-bearing message, so we PUT an
        update to it (no attachments → the card and its buttons disappear). Best-effort:
        on any failure we fall back to a plain confirmation reply, never crashing.
        """
        import httpx

        service_url = (activity.get("serviceUrl") or "").rstrip("/")
        conv = (activity.get("conversation") or {}).get("id")
        reply_to = activity.get("replyToId")
        if not (service_url and conv and reply_to):
            self._reply(activity, text, None)  # nothing to update, just confirm
            return
        try:
            token = self._connector_token()
        except Exception as exc:  # noqa: BLE001 - token fetch failed; nothing sent
            logger.error("kai_teams_token_failed err=%s: %s", type(exc).__name__, exc)
            return
        url = f"{service_url}/v3/conversations/{conv}/activities/{reply_to}"
        try:
            httpx.put(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"type": "message", "text": text},
                timeout=30.0,
            ).raise_for_status()
        except Exception as exc:  # noqa: BLE001 - log, never crash the webhook
            logger.error("kai_teams_card_retire_failed err=%s: %s", type(exc).__name__, exc)
            self._reply(activity, text, None)  # fall back to a plain confirmation

    # ---- the webhook server ------------------------------------------------ #
    def run(self) -> None:
        import uvicorn
        from fastapi import FastAPI, Header, HTTPException, Response

        app = FastAPI(title="KAI Teams bot", docs_url=None, redoc_url=None, openapi_url=None)

        @app.post("/api/messages")
        def messages(activity: dict, authorization: str = Header(default="")):  # noqa: ANN202
            try:
                self._validate(authorization, expected_service_url=activity.get("serviceUrl", ""))
            except Exception as exc:  # noqa: BLE001
                logger.warning("kai_teams_auth_rejected err=%s", type(exc).__name__)
                raise HTTPException(status_code=401, detail="invalid bot token") from exc

            kind, msg, fb = parse_activity(activity)
            if kind == "feedback":
                ack = self._service.handle_feedback(fb)
                # Retire the tapped card so feedback can't be submitted repeatedly:
                # Teams does NOT auto-disable Action.Submit, so the buttons stay live
                # otherwise (parity with Slack's chat_update / Webex's delete-on-tap).
                self._retire_card(activity, ack or "Thanks, feedback received.")
                return Response(status_code=200)
            if kind == "message":
                if is_help_request(msg.text):
                    self._reply(activity, HELP_TEXT, None)
                    return Response(status_code=200)
                data, err = self._service.answer(msg)
                text = err if data is None else format_reply(data)
                card = (
                    feedback_card(msg.text, escalate_only=bool(data.get("escalated")))
                    if (self._show_card and data)
                    else None
                )
                self._reply(activity, text, card)
            return Response(status_code=200)

        logger.info("kai_teams_starting on %s:%d (POST /api/messages)", self._host, self._port)
        uvicorn.run(app, host=self._host, port=self._port)
