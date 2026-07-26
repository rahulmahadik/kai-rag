#!/usr/bin/env python3
"""Adversarial output-quality inspection, runs real questions through the live
/ask API and flags USER-FACING issues the accuracy eval doesn't catch:

  - citation markers that don't match the Sources list (count/contiguity)
  - answers truncated mid-sentence (max_tokens too low)
  - editorializing openers ("Based on the provided context...")
  - formatting artifacts (double spaces, empty brackets, dangling punctuation)
  - verbatim sentence repetition
  - excessive length

Prints a per-question report + a summary of issue counts. Read-only; hits the
running server.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib import request as _req

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kai.config import get_settings

QUESTIONS = [
    "How do I use the replication tool?",
    "How does partition leader election work in Kafka?",
    "What is Kafka?",
    "What are the responsibilities of a Kafka maintainer?",
    "How are consumer offsets managed in Kafka?",
    "Tell me everything about Kafka replication in full detail.",
    "What is the controller and how does it handle leader changes and topic deletion?",
    "replication",
    "What does the FAQ say about message ordering?",
    "List all the Kafka command line tools and what each does.",
    "What is the request purgatory and how does it work internally?",
    "How do I migrate from 0.7 to 0.8?",
    # edge cases
    "Kafka",
    "Compare the controller and the request purgatory.",
    "What are ALL the ZooKeeper paths Kafka uses? List every one.",
    "why replication?",
    "Explain Kafka replication, then the controller, then leader election, in detail.",
    "What is the difference between a leader and a follower replica?",
    "Summarize the bylaws in one sentence.",
    "What ports and config values does Kafka use?",
]


def ask(url, q, key, timeout):
    body = json.dumps({"question": q}).encode()
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    with _req.urlopen(
        _req.Request(url.rstrip("/") + "/ask", data=body, headers=h, method="POST"), timeout=timeout
    ) as r:
        return json.loads(r.read().decode())


def flags(ans: str, cites: list) -> list[str]:
    out = []
    markers = [int(x) for x in re.findall(r"\[(\d+)\]", ans)]
    uniq = sorted(set(markers))
    if markers:
        if max(markers) > len(cites):
            out.append(f"MARKER>SOURCES (max [{max(markers)}] but {len(cites)} sources)")
        if uniq != list(range(1, len(uniq) + 1)):
            out.append(f"MARKERS_NONCONTIGUOUS {uniq}")
    stripped = ans.rstrip()
    if stripped and stripped[-1] not in ".!?\"'`)]:" and not stripped.endswith("]"):
        out.append("TRUNCATED_MIDSENTENCE")
    low = ans.lstrip().lower()
    for op in (
        "based on the provided context",
        "based on the context",
        "according to the provided",
        "the provided context",
        "based on the information provided",
    ):
        if low.startswith(op):
            out.append("EDITORIAL_OPENER")
            break
    # Whitespace/punctuation artifacts are judged on PROSE only: code spans keep
    # legitimate empty parens (`commitSync()`), and line-leading runs are markdown
    # indentation (nested lists / code blocks), both are CORRECT output that the
    # code-aware tidy deliberately preserves.
    prose_segs = [
        seg
        for i, seg in enumerate(re.split(r"(```.*?(?:```|$)|`[^`\n]+`)", ans, flags=re.DOTALL))
        if i % 2 == 0
    ]
    if any(re.search(r"\S[ \t]{2,}\S", seg) for seg in prose_segs):  # intra-line run
        out.append("DOUBLE_SPACE")
    # Empty parens/brackets count as artifacts only when FREE-STANDING, glued to
    # an identifier they are function/array references (`brokerStartup()`), which
    # are correct content the tidy deliberately preserves even unbackticked.
    if any(
        re.search(r"(?<!\S)\[\s*\]|(?<!\S)\(\s*\)|[ \t][.,;]| ,|\.\.(?!\.)", seg)
        for seg in prose_segs
    ):
        out.append("PUNCT_ARTIFACT")
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", ans) if len(s.strip()) > 25]
    if len(sents) != len(set(sents)):
        out.append("REPEATED_SENTENCE")
    if len(ans) > 2600:
        out.append(f"VERY_LONG({len(ans)}chars)")
    return out


def main():
    s = get_settings()
    url, key, timeout = s.kai_api_url, s.api_key, float(s.llm_timeout) + 60
    issue_counts: dict[str, int] = {}
    print(f"inspecting {len(QUESTIONS)} questions\n")
    for i, q in enumerate(QUESTIONS, 1):
        try:
            d = ask(url, q, key, timeout)
        except Exception as e:
            print(f"[{i}] ERROR {type(e).__name__}: {e}")
            continue
        if d.get("escalated"):
            print(f"[{i}] ESCALATED conf={d['confidence']:.3f}  {q[:50]}")
            continue
        f = flags(d["answer"], d.get("citations") or [])
        for x in f:
            issue_counts[x.split("(")[0].split(" ")[0]] = (
                issue_counts.get(x.split("(")[0].split(" ")[0], 0) + 1
            )
        tag = "  ".join(f) if f else "clean"
        print(
            f"[{i}] {('ISSUES: ' + tag) if f else 'clean'}  | conf={d['confidence']:.3f} cites={len(d.get('citations') or [])} len={len(d['answer'])}  | {q[:46]}"
        )
    print("\n=== ISSUE SUMMARY ===")
    if not issue_counts:
        print("  no issues flagged")
    for k, v in sorted(issue_counts.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
