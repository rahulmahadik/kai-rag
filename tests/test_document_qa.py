"""Ad-hoc document Q&A: extract_text + answer_from_document + /ask-document.

A dropped file is held to the SAME never-fabricate bar as the corpus — relevant
questions answer from the doc, irrelevant ones say "not in the document".
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

import kai.app as app_module
from kai.config import Settings
from kai.pipeline.ask import answer_from_document
from kai.providers.file_source import extract_text

DOC = (
    "Kafka is a distributed commit log for streaming data. "
    "Replication copies partition data from leaders to followers for durability. " * 6
)


class KeywordEmbedder:
    """Cosine ~1 between texts sharing the 'kafka/replication' topic, ~0 otherwise."""

    dimensions = 3

    def embed(self, texts):
        out = []
        for t in texts:
            low = t.lower()
            out.append(
                [
                    1.0 if ("kafka" in low or "replicat" in low or "leader" in low) else 0.0,
                    1.0 if ("france" in low or "capital" in low or "weather" in low) else 0.0,
                    0.05,  # tiny shared component so norms are non-zero
                ]
            )
        return out


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    def complete(self, system, user, *, max_tokens=1024, temperature=0.1):
        return self.reply


def _settings(**o):
    base = dict(
        verify_answers=False,
        multi_query=False,
        reranker="noop",
        confidence_threshold=0.45,
        answer_grounding_min=0.0,
        database_url="",
    )
    base.update(o)  # let a test override any default (e.g. confidence_threshold)
    return Settings(_env_file=None, **base)


# ---- extract_text ----
def test_extract_text_plain_and_binary():
    assert "hello world" in extract_text("a.txt", b"hello world")
    assert extract_text("blob.txt", bytes(range(256)) * 8) == ""  # binary -> nothing


def test_extract_text_utf16():
    assert "Información" in extract_text("u.txt", "Información".encode("utf-16"))


# ---- answer_from_document ----
def test_document_relevant_question_answers():
    ans = answer_from_document(
        "How does replication work?",
        DOC,
        "kafka.txt",
        KeywordEmbedder(),
        FakeLLM("Replication copies partition data to followers [1]."),
        _settings(),
    )
    assert not ans.escalated and "replication" in ans.answer.lower()


def test_document_irrelevant_question_says_not_found():
    ans = answer_from_document(
        "What is the capital of France?",
        DOC,
        "kafka.txt",
        KeywordEmbedder(),
        FakeLLM("Paris."),
        _settings(),
    )
    assert ans.escalated and "kafka.txt" in ans.answer


def test_confidence_threshold_actually_gates():
    # Same relevant question + sources: an unreachably-high CONFIDENCE_THRESHOLD must
    # escalate, a zero threshold must answer — proving the config knob takes effect.
    args = (
        "How does replication work?",
        DOC,
        "kafka.txt",
        KeywordEmbedder(),
        FakeLLM("Replication copies data to followers [1]."),
    )
    hi = answer_from_document(*args, _settings(confidence_threshold=1.1))
    lo = answer_from_document(*args, _settings(confidence_threshold=0.0))
    assert hi.escalated and not lo.escalated


def test_document_empty_text():
    ans = answer_from_document(
        "anything", "   ", "empty.txt", KeywordEmbedder(), FakeLLM("x"), _settings()
    )
    assert ans.escalated


# ---- /ask-document endpoint ----
@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "build_providers",
        lambda s: (
            KeywordEmbedder(),
            FakeLLM("Replication copies data to followers [1]."),
            object(),
            object(),
            object(),
        ),
    )
    s = Settings(
        _env_file=None,
        KAI_API_KEY="k",
        reranker="noop",
        database_url="",
        verify_answers=False,
        multi_query=False,
        answer_grounding_min=0.0,
        answer_cache_size=0,
        file_max_bytes=1000,
    )
    return TestClient(app_module.create_app(s), raise_server_exceptions=False)


AUTH = {"Authorization": "Bearer k"}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_ask_document_endpoint_answers(client):
    r = client.post(
        "/ask-document",
        headers=AUTH,
        json={
            "question": "How does replication work?",
            "filename": "kafka.txt",
            "content_b64": _b64(DOC.encode()),
        },
    )
    assert r.status_code == 200
    assert not r.json()["escalated"]


def test_ask_document_bad_base64_is_422(client):
    r = client.post(
        "/ask-document",
        headers=AUTH,
        json={"question": "q", "filename": "x.txt", "content_b64": "!!!not base64!!!"},
    )
    assert r.status_code == 422


def test_ask_document_oversize_is_413(client):
    r = client.post(
        "/ask-document",
        headers=AUTH,
        json={"question": "q", "filename": "big.txt", "content_b64": _b64(b"x" * 2000)},
    )
    assert r.status_code == 413


def test_ask_document_requires_auth(client):
    r = client.post(
        "/ask-document", json={"question": "q", "filename": "x.txt", "content_b64": _b64(b"hi")}
    )
    assert r.status_code == 401


def test_ask_document_unsupported_type_is_clear(client):
    # A .docx (we have no parser) must say so by name — not a vague "couldn't read".
    r = client.post(
        "/ask-document",
        headers=AUTH,
        json={"question": "q", "filename": "report.docx", "content_b64": _b64(b"PK\x03\x04zip")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["escalated"] and ".docx" in body["answer"]


def test_ask_document_scanned_pdf_suggests_ocr(client):
    # A .pdf with no extractable text → scanned/image PDF guidance (OCR), not docx-style.
    r = client.post(
        "/ask-document",
        headers=AUTH,
        json={"question": "q", "filename": "scan.pdf", "content_b64": _b64(b"not a real pdf")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["escalated"] and "OCR" in body["answer"]
