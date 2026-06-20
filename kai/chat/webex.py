"""Webex adapter — the transport-specific half of the KAI chat bot.

Outbound websocket (webex_bot SDK): no public URL / firewall change needed. All
"what to answer" logic lives in :class:`kai.chat.service.ChatService`; this file
only handles Webex specifics — receiving @mentions/card-actions, the typing-ack
substitute (pre_execute), threaded replies (SDK ``threads=True``), the 7439-byte
message split, the Adaptive Card feedback affordance, and the supervised reconnect
loop. The ``webex_bot`` SDK is imported lazily inside :meth:`run`.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from urllib.parse import unquote

import httpx

from kai.chat.base import FeedbackEvent, IncomingMessage
from kai.chat.service import (
    HELP_TEXT,
    ChatService,
    format_reply,
    is_help_request,
    split_message,
)
from kai.config import Settings

logger = logging.getLogger("kai.chat.webex")

# Webex rejects messages whose markdown exceeds 7439 bytes; leave headroom.
WEBEX_MARKDOWN_LIMIT = 7000
_WEBEX_API = "https://webexapis.com/v1/messages"


# --------------------------------------------------------------------------- #
# Raw Webex REST helpers (edit + DM are not in the SDK; call the API directly
# with the bot token). All return-or-None / bool, never raise — the caller
# falls back to the SDK's normal reply path on any failure.
# --------------------------------------------------------------------------- #
def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def create_message(token: str, **fields) -> str | None:
    """POST /messages (roomId+markdown, optional parentId / toPersonEmail). Returns id."""

    try:
        r = httpx.post(
            _WEBEX_API,
            json={k: v for k, v in fields.items() if v},
            headers=_hdr(token),
            timeout=20.0,
        )
        if r.status_code in (200, 201):
            return r.json().get("id")
        logger.warning("kai_webex_create status=%d", r.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kai_webex_create err=%s", type(exc).__name__)
    return None


def edit_message(token: str, message_id: str, room_id: str, markdown: str) -> bool:
    """PUT /messages/{id} — edit a bot message in place (the ack → the answer).

    Not in the SDK. Caveats (Webex): ≤10 edits/message; a message bearing a
    file/Adaptive-Card attachment cannot be edited (so the feedback card is a
    separate follow-up message).
    """

    try:
        r = httpx.put(
            f"{_WEBEX_API}/{message_id}",
            json={"roomId": room_id, "markdown": markdown},
            headers=_hdr(token),
            timeout=20.0,
        )
        if r.status_code == 200:
            return True
        logger.warning("kai_webex_edit status=%d", r.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kai_webex_edit err=%s", type(exc).__name__)
    return False


def delete_message(token: str, message_id: str) -> bool:
    """DELETE /messages/{id} — remove a bot message (e.g. a stale 'Searching…' ack
    when an in-place edit failed and we're posting the answer fresh). Best-effort."""

    try:
        r = httpx.delete(f"{_WEBEX_API}/{message_id}", headers=_hdr(token), timeout=20.0)
        if r.status_code in (200, 204):
            return True
        logger.warning("kai_webex_delete status=%d", r.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kai_webex_delete err=%s", type(exc).__name__)
    return False


def send_direct_message(token: str, person_email: str, markdown: str) -> bool:
    """POST /messages with toPersonEmail — proactively DM a user (1:1).

    Webex auto-creates/reuses the 1:1 space. The user must have messaged the bot
    at least once (bots can't cold-DM strangers) — true for anyone KAI answered.
    """

    return create_message(token, toPersonEmail=person_email, markdown=markdown) is not None


def get_message_files(token: str, message_id: str) -> list[str]:
    """Return the authenticated file URLs attached to an incoming message (or [])."""

    try:
        r = httpx.get(f"{_WEBEX_API}/{message_id}", headers=_hdr(token), timeout=20.0)
        if r.status_code == 200:
            return list(r.json().get("files") or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("kai_webex_getmsg err=%s", type(exc).__name__)
    return []


def get_message_parent(token: str, message_id: str) -> str:
    """Return a message's thread root (its ``parentId``), or the id itself if it is a
    root. Used to post a feedback ack as a reply IN the original thread rather than as
    a disconnected new message (a card-bearing message can't be edited in Webex)."""

    try:
        r = httpx.get(f"{_WEBEX_API}/{message_id}", headers=_hdr(token), timeout=20.0)
        if r.status_code == 200:
            return r.json().get("parentId") or message_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("kai_webex_getparent err=%s", type(exc).__name__)
    return message_id


def download_file(token: str, url: str, max_bytes: int = 0) -> tuple[str, bytes] | None:
    """Download a Webex file (bot-token auth). Returns (filename, bytes) or None.

    Streams the body so an oversized attachment is rejected (by Content-Length, then
    mid-download) WITHOUT buffering the whole file into the bot's memory first —
    ``max_bytes<=0`` disables the cap.
    """

    try:
        with httpx.stream(
            "GET",
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
            follow_redirects=True,
        ) as r:
            if r.status_code != 200:
                return None
            clen = r.headers.get("Content-Length", "")
            if max_bytes > 0 and clen.isdigit() and int(clen) > max_bytes:
                logger.warning("kai_webex_file_too_large size=%s limit=%d", clen, max_bytes)
                return None
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if max_bytes > 0 and len(buf) > max_bytes:
                    logger.warning("kai_webex_file_too_large streamed limit=%d", max_bytes)
                    return None
            cd = r.headers.get("Content-Disposition", "")
            name = "document"
            # Prefer RFC 5987 filename* (UTF-8, percent-encoded) then plain filename=.
            star = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.IGNORECASE)
            raw = unquote(star.group(1).strip()) if star else ""
            if not raw and "filename=" in cd:
                raw = cd.split("filename=")[-1].strip().strip('"')
            if raw:
                # Sanitize the server-supplied name before it rides into reply markdown:
                # basename only (drop any path), printable chars, no newlines, bounded.
                raw = raw.replace("\\", "/").split("/")[-1]
                raw = "".join(c for c in raw if c.isprintable() and c not in "\r\n")[:200]
                name = raw or name
            return name, bytes(buf)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kai_webex_download err=%s", type(exc).__name__)
    return None


def feedback_card(question: str, *, escalate_only: bool = False) -> dict:
    """Compact Adaptive Card routing taps back via ``callback_keyword``.

    Confident answer → 👍 / 👎 / escalate-anyway. Escalated answer (``escalate_only``)
    → a single **Escalate to a human** action (no answer was given, so thumbs don't
    apply). Portable to Teams (same Action.Submit shape).
    """

    def _action(title: str, verdict: str) -> dict:
        return {
            "type": "Action.Submit",
            "title": title,
            "data": {
                "callback_keyword": "kai_feedback",
                "verdict": verdict,
                "question": question[:500],
            },
        }

    if escalate_only:
        body = [
            {
                "type": "TextBlock",
                "text": "Need a person on this?",
                "size": "Small",
                "isSubtle": True,
            }
        ]
        actions = [_action("🙋 Escalate to a human", "escalate")]
    else:
        body = [
            {"type": "TextBlock", "text": "Was this helpful?", "size": "Small", "isSubtle": True}
        ]
        actions = [
            _action("👍 Yes", "up"),
            _action("👎 No", "down"),
            _action("Escalate anyway", "escalate"),
        ]

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.3",
        "body": body,
        "actions": actions,
    }


def webex_reply(ask_json: dict, question: str, *, show_card: bool) -> tuple[list[str], dict | None]:
    """Compute the Webex message pieces + optional feedback card — PURE, testable.

    Always returns at least one (possibly empty) piece. When ``show_card`` is on, a
    confident answer rides the 👍/👎 card; an escalation rides a single "Escalate to a
    human" card instead.
    """

    pieces = split_message(format_reply(ask_json), WEBEX_MARKDOWN_LIMIT) or [""]
    escalated = bool(ask_json.get("escalated"))
    card = feedback_card(question, escalate_only=escalated) if show_card else None
    return pieces, card


def memory_key(room: str | None, parent: str | None, activity: dict) -> tuple:
    """Conversation-memory key — PURE, testable.

    Keyed by ``(thread/room, sender)`` so two people talking to the bot in ONE
    thread keep SEPARATE follow-up context (no cross-user leakage).
    """

    sender = (activity.get("actor") or {}).get("emailAddress") or ""
    return (parent or room, sender)


class WebexAdapter:
    """:class:`kai.chat.base.ChatAdapter` for Cisco Webex (websocket)."""

    name = "webex"

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._service = ChatService(settings)

    def run(self) -> None:  # noqa: C901 — one cohesive transport setup
        settings, service = self._s, self._service
        token = settings.webex_bot_token.strip()
        if not token:
            raise SystemExit(
                "WEBEX_BOT_TOKEN is not set. Create a bot at developer.webex.com "
                "(My Webex Apps → Create a Bot), copy its access token, and set it in .env."
            )

        from webex_bot.models.command import Command  # lazy: SDK only needed to run
        from webex_bot.models.response import Response
        from webex_bot.webex_bot import WebexBot

        show_card = getattr(settings, "webex_feedback_card", False)

        def _responses(pieces: list[str], card: dict | None):
            out = []
            for p in pieces:
                r = Response()
                r.markdown = p
                out.append(r)
            if card is not None and out:
                out[-1].attachments = {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card,
                }
            if not out:
                return "I don't have an answer for that."
            return out[0].markdown if (len(out) == 1 and card is None) else out

        token = settings.webex_bot_token.strip()
        edit_in_place = getattr(settings, "webex_edit_in_place", True)
        use_memory = getattr(settings, "conversation_memory", False)
        ACK = getattr(settings, "bot_ack_message", None) or "🔎 _Searching the knowledge base…_"
        convo: dict[tuple, str] = {}  # (thread/room, sender) -> last question (bounded)
        convo_lock = threading.Lock()  # the SDK runs execute() across worker threads

        def _room_thread(activity):  # noqa: ANN001 — best-effort id extraction
            room = (activity.get("target") or {}).get("globalId") or activity.get("roomId")
            parent = (activity.get("parent") or {}).get("id") or activity.get("id")
            return room, parent

        def _enrich(text: str, prev: str | None) -> str:
            """Prepend the PREVIOUS question's topic to a referential follow-up so it
            retrieves in context. Pure (``prev`` is read under the lock by the caller,
            atomically with the write). The enriched query still runs the FULL gate +
            guards — context can never make an out-of-scope question fabricate."""

            if not prev:
                return text
            low = text.lower().strip()
            words = low.split()
            referential = any(
                low.startswith(p)
                for p in (
                    "and ",
                    "what about",
                    "how about",
                    "what else",
                    "tell me more",
                    "more about",
                    "also ",
                    "what's ",
                    "explain ",
                )
            ) or (
                len(words) <= 4
                and any(
                    w in {"it", "that", "this", "they", "those", "these", "them"} for w in words
                )
            )
            return f"{text} (in the context of: {prev})" if (prev and referential) else text

        class _AskCommand(Command):
            def __init__(self) -> None:
                super().__init__(
                    command_keyword=None,
                    help_message="Ask me anything about the knowledge base.",
                    # When editing in place we manage the ack ourselves,
                    # so the SDK must NOT post/delete a pre_execute reply.
                    delete_previous_message=not edit_in_place,
                )

            def pre_execute(self, message, attachment_actions, activity):  # noqa: ANN001
                if edit_in_place or not (message or "").strip():
                    return None  # ack handled in execute() (edited later), or nothing to do
                return "🔎 On it — searching the knowledge base…"

            def execute(self, message, attachment_actions, activity):  # noqa: ANN001
                text = (message or "").strip()
                if not text:
                    return (
                        "Hi! I'm KAI. Ask me anything about the knowledge base and I'll "
                        "answer with sources — type **help** to see what I can do, or "
                        "attach a file and ask about it."
                    )
                if is_help_request(text):
                    return HELP_TEXT
                logger.info("kai_bot_question len=%d", len(text))

                room, parent = _room_thread(activity)

                # Inbound file Q&A: if the user attached a file, answer about THAT
                # document (ad-hoc RAG) instead of the corpus. A file the user clearly
                # MEANT to ask about must never silently fall through to corpus Q&A —
                # that would look like the attachment was ignored. (We can only fail to
                # LIST the attachments — that path falls through to corpus Q&A.)
                msg_id = activity.get("id")
                if token and msg_id:
                    try:
                        files = get_message_files(token, msg_id)
                    except Exception:  # noqa: BLE001
                        files = []
                    if files:
                        max_bytes = int(getattr(settings, "file_max_bytes", 0) or 0)
                        dl = download_file(token, files[0], max_bytes=max_bytes)
                        if not dl:
                            # A file WAS attached but we couldn't read it (too large, or
                            # the download failed) — say so rather than quietly answering
                            # the question against the corpus and ignoring the file.
                            return (
                                "Sorry — I couldn't read that attachment. It may be larger "
                                "than I can handle; try a smaller file, or ask me in text."
                            )
                        fname, data = dl
                        doc, derr = service.ask_document(fname, data, text)
                        reply_md = derr if doc is None else format_reply(doc)
                        if doc is not None and not doc.get("escalated"):
                            # Make the scope explicit: the file was used ONLY for this
                            # question, in this thread — it's not stored or added to the KB.
                            reply_md += (
                                f"\n\n_(Answered from **{fname}** just for this question in "
                                "this thread — the file isn't stored or added to the "
                                "knowledge base.)_"
                            )
                        pieces = split_message(reply_md, WEBEX_MARKDOWN_LIMIT) or [""]
                        # The document answer is ALREADY computed, so there's nothing to
                        # wait on — post it directly IN-THREAD (parentId). No ack to edit
                        # (which could dangle as a stray "Searching…" if the edit failed).
                        if token and room:
                            for p in pieces:
                                create_message(token, roomId=room, parentId=parent, markdown=p)
                            return None
                        return _responses(pieces, None)
                # key memory by (thread, sender) so two people in ONE thread keep
                # separate context (no cross-user leakage); guarded for the threadpool.
                key = memory_key(room, parent, activity)
                prev = None
                if use_memory and (parent or room):
                    # Read the prior question AND store the current one in ONE critical
                    # section, so a fast follow-up in the same thread can't be enriched
                    # with a stale/own topic (read-modify-write is atomic per key).
                    with convo_lock:
                        prev = convo.get(key)
                        convo[key] = text
                        if len(convo) > 500:  # bound the memory
                            convo.pop(next(iter(convo)))
                query = _enrich(text, prev)

                # Edit-in-place: post the ack NOW, then edit it into the answer when
                # ready — no delete/repost churn, keeps thread position. Falls back to
                # a normal reply if anything in the direct-REST path fails.
                ack_id = None
                if edit_in_place and token and room:
                    ack_id = create_message(token, roomId=room, parentId=parent, markdown=ACK)

                data, err = service.answer(IncomingMessage(text=query, thread_id=parent))
                if data is None:
                    if ack_id and room and edit_message(token, ack_id, room, err):
                        return None
                    return err
                pieces, card = webex_reply(data, text, show_card=show_card)

                if ack_id and room:
                    # Edit the ack into the first piece ("Here's what I found:" framing),
                    # post any overflow pieces, then the card as a separate message
                    # (a card-bearing message can't be edited). Never prepend the
                    # "Here's what I found:" framing to an ESCALATION — it didn't answer.
                    prefix = (
                        ""
                        if data.get("escalated")
                        else (getattr(settings, "bot_answer_prefix", "") or "")
                    )
                    answer_md = (f"**{prefix}**\n\n" + pieces[0]) if prefix else pieces[0]
                    if edit_message(token, ack_id, room, answer_md):
                        for p in pieces[1:]:
                            create_message(token, roomId=room, parentId=parent, markdown=p)
                        if card is not None:
                            # card can't ride on the edited message — post it as a
                            # small in-thread follow-up via the SDK return value.
                            label = (
                                "_Need a person on this?_"
                                if data.get("escalated")
                                else "_Rate this answer:_"
                            )
                            return _responses([label], card)
                        return None
                    # edit failed — delete the stale ack so it doesn't dangle, then
                    # fall through to a normal (fresh) reply.
                    delete_message(token, ack_id)
                return _responses(pieces, card)

        class _FeedbackCommand(Command):
            def __init__(self) -> None:
                # We dismiss the card AND post the ack ourselves (as an in-thread reply),
                # so don't let the SDK auto-delete or auto-post a top-level message.
                super().__init__(
                    command_keyword="kai_feedback_kw",
                    card_callback_keyword="kai_feedback",
                    help_message="(internal) answer feedback",
                    delete_previous_message=False,
                )

            def execute(self, message, attachment_actions, activity):  # noqa: ANN001
                inputs = getattr(attachment_actions, "inputs", None) or {}
                actor = ""
                try:
                    actor = activity["actor"].get("emailAddress", "")
                except Exception:  # noqa: BLE001
                    pass
                ack = (
                    service.handle_feedback(
                        FeedbackEvent(
                            verdict=(inputs.get("verdict") or "").strip(),
                            question=(inputs.get("question") or "").strip(),
                            sender_email=actor,
                        )
                    )
                    or ""
                )
                card_id = getattr(attachment_actions, "messageId", "") or ""
                room = getattr(attachment_actions, "roomId", "") or ""
                # Reply with the ack IN the original thread (a card message can't be
                # edited), then dismiss the tapped card so it can't be submitted twice.
                if ack and room:
                    parent = get_message_parent(token, card_id) if card_id else ""
                    create_message(token, roomId=room, parentId=parent or None, markdown=ack)
                if card_id:
                    delete_message(token, card_id)
                return None  # we already posted; don't let the SDK add a top-level msg

        approved_domains = [
            d.strip() for d in settings.webex_approved_domains.split(",") if d.strip()
        ]
        approved_users = [u.strip() for u in settings.webex_approved_users.split(",") if u.strip()]
        if approved_domains or approved_users:
            logger.info(
                "kai_bot_access_restricted domains=%d users=%d",
                len(approved_domains),
                len(approved_users),
            )
        else:
            logger.warning(
                "kai_bot_open — answering anyone in the space. Set "
                "WEBEX_APPROVED_DOMAINS / WEBEX_APPROVED_USERS to restrict."
            )

        # Supervised reconnect loop: a websocket crash that escapes the SDK's own
        # retry would otherwise kill the bot silently.
        backoff = 5.0
        while True:
            try:
                bot = WebexBot(
                    teams_bot_token=token,
                    bot_name="KAI",
                    approved_domains=approved_domains or None,
                    approved_users=approved_users or None,
                    help_command=_AskCommand(),
                )
                bot.add_command(_FeedbackCommand())
                logger.info("kai_bot_starting api=%s", settings.kai_api_url)
                backoff = 5.0
                bot.run()
                logger.warning("kai_bot_stopped — restarting in %.0fs", backoff)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 — supervised: log, back off, retry
                logger.error(
                    "kai_bot_crashed err=%s: %s — restarting in %.0fs",
                    type(exc).__name__,
                    exc,
                    backoff,
                )
            time.sleep(backoff)
            backoff = min(backoff * 2, 300.0)
