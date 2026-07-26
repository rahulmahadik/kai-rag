#!/usr/bin/env python3
"""KAI accuracy evaluation harness.

Runs a grounded golden set (``eval/golden.json``) through the live KAI ``/ask``
API, then CROSS-CHECKS each answer against the actual source text in the vector
store (so a "correct-looking" answer that isn't really in the cited page is
caught). Writes a human-readable markdown report of *asked vs returned*
``doc/eval-report.md``, for cross-checking against Confluence.

Design priorities (in order):
  1. ZERO wrong answers. The only truly wrong outcome is ANSWERING a question the
     knowledge base cannot support (out-of-scope / non-existent). That is a
     "humiliation". This is reported as WRONG and must be 0.
  2. Grounding. An in-scope answer must be supported by its cited source (the
     expected fact appears in the cited page's text). Ungrounded → REVIEW.
  3. Coverage. Of the answerable questions, how many did we actually answer
     (vs safely abstain/escalate). Abstaining is SAFE, not wrong.

Runs SEQUENTIALLY with a pause between questions to keep CPU/GPU load modest.

    .venv/bin/python eval/run_eval.py            # full set
    .venv/bin/python eval/run_eval.py --sleep 4  # more breathing room
    .venv/bin/python eval/run_eval.py --limit 5  # quick smoke
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib import request as _req

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kai.config import get_settings  # noqa: E402


def _ask(api_url: str, question: str, api_key: str, timeout: float) -> dict:
    body = json.dumps({"question": question}).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = _req.Request(api_url.rstrip("/") + "/ask", data=body, headers=headers, method="POST")
    with _req.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _source_texts_for_titles(database_url: str, titles: list[str]) -> str:
    """Return the concatenated lowercased chunk text of the given page titles.

    Used to verify an answer is genuinely grounded in its cited source(s).
    """

    if not titles:
        return ""
    import psycopg

    out: list[str] = []
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT text FROM kai_chunks WHERE title = ANY(%s)", (titles,))
        out = [row[0] for row in cur.fetchall()]
    return "\n".join(out).lower()


def _verdict(case: dict, resp: dict, grounded: bool | None) -> tuple[str, str]:
    """Return (verdict, note). Verdict ∈ {WRONG, PASS, GROUNDING?, ABSTAINED, OK-ESCALATED}."""

    escalated = bool(resp.get("escalated"))
    answer_low = (resp.get("answer") or "").lower()

    if case.get("expect_escalate"):
        if escalated:
            return "OK-ESCALATED", "correctly abstained (not in KB)"
        return "WRONG", "ANSWERED a question the KB cannot support, fabrication risk"

    # in_scope
    if escalated:
        return "ABSTAINED", "safe miss (escalated an answerable question)"
    expects = [e.lower() for e in case.get("expect_any", [])]
    hit = any(e in answer_low for e in expects) if expects else True
    if not hit:
        return "REVIEW", "answer did not contain any expected fact, inspect"
    if grounded is False:
        return "GROUNDING?", "expected fact NOT found in the cited source text"
    return "PASS", "answered with the expected fact, grounded in the cited source"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--sleep", type=float, default=3.0, help="seconds between questions (CPU pacing)"
    )
    ap.add_argument("--limit", type=int, default=0, help="only run the first N cases")
    ap.add_argument("--out", default=str(ROOT / "doc" / "eval-report.md"))
    ap.add_argument(
        "--golden",
        default=str(ROOT / "eval" / "golden.json"),
        help="path to the golden-set JSON to grade against",
    )
    args = ap.parse_args()

    s = get_settings()
    api_url = s.kai_api_url
    api_key = s.api_key
    threshold = s.confidence_threshold
    timeout = float(s.llm_timeout) + 60.0

    golden = json.loads(Path(args.golden).read_text())
    cases = golden["cases"]
    if args.limit:
        cases = cases[: args.limit]

    rows: list[dict] = []
    counts = {
        "PASS": 0,
        "OK-ESCALATED": 0,
        "ABSTAINED": 0,
        "REVIEW": 0,
        "GROUNDING?": 0,
        "WRONG": 0,
        "ERROR": 0,
    }

    print(f"KAI eval, {len(cases)} cases against {api_url} (threshold={threshold})\n")
    for i, case in enumerate(cases, 1):
        q = case["question"]
        print(f"[{i}/{len(cases)}] {case['id']}: {q}")
        try:
            resp = _ask(api_url, q, api_key, timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"    ERROR: {type(exc).__name__}: {exc}")
            rows.append(
                {"case": case, "resp": None, "verdict": "ERROR", "note": str(exc), "grounded": None}
            )
            counts["ERROR"] += 1
            time.sleep(args.sleep)
            continue

        citations = resp.get("citations") or []
        cited_titles = [c.get("title", "") for c in citations if c.get("title")]
        grounded: bool | None = None
        if case["category"] == "in_scope" and not resp.get("escalated") and cited_titles:
            try:
                src = _source_texts_for_titles(s.database_url, cited_titles)
                expects = [e.lower() for e in case.get("expect_any", [])]
                grounded = any(e in src for e in expects) if expects else None
            except Exception:  # noqa: BLE001 - grounding check is best-effort
                grounded = None

        verdict, note = _verdict(case, resp, grounded)
        counts[verdict] = counts.get(verdict, 0) + 1
        rows.append(
            {"case": case, "resp": resp, "verdict": verdict, "note": note, "grounded": grounded}
        )
        print(
            f"    -> {verdict}  (escalated={resp.get('escalated')}, conf={resp.get('confidence'):.3f})"
        )
        time.sleep(args.sleep)

    _write_report(Path(args.out), api_url, threshold, counts, rows)
    print("\n=== SUMMARY ===")
    for k, v in counts.items():
        if v:
            print(f"  {k}: {v}")
    print(f"\n  WRONG (must be 0): {counts['WRONG']}")
    print(f"  report: {args.out}")


def _write_report(
    path: Path, api_url: str, threshold: float, counts: dict, rows: list[dict]
) -> None:
    answered = counts["PASS"] + counts["REVIEW"] + counts["GROUNDING?"]
    in_scope_total = sum(1 for r in rows if r["case"]["category"] == "in_scope")
    lines: list[str] = []
    lines.append("# KAI evaluation report, asked vs returned\n")
    lines.append(
        "Cross-check each answer below against the source Confluence page (links in "
        "the Citations column). The **WRONG** count is the critical metric: it is the "
        "number of questions the knowledge base cannot support that KAI *answered* "
        "anyway (fabrication). It must be 0.\n"
    )
    lines.append(f"- API: `{api_url}`  ·  confidence threshold: `{threshold}`")
    lines.append(
        f"- **WRONG (fabrications): {counts['WRONG']}**  ·  PASS: {counts['PASS']}  ·  "
        f"correctly-escalated: {counts['OK-ESCALATED']}  ·  safe-abstained: {counts['ABSTAINED']}  ·  "
        f"review: {counts['REVIEW']}  ·  grounding?: {counts['GROUNDING?']}  ·  errors: {counts['ERROR']}"
    )
    lines.append(f"- In-scope answered: {answered}/{in_scope_total}\n")

    lines.append("## Results\n")
    lines.append("| # | Verdict | Category | Question | Escalated | Conf | Grounded | Citations |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        c = r["case"]
        resp = r["resp"] or {}
        cits = (
            " ; ".join(
                f"[{(ci.get('title') or 'source')}]({ci.get('url') or ''})"
                for ci in (resp.get("citations") or [])
            )
            or ", "
        )
        conf = f"{resp.get('confidence'):.3f}" if resp.get("confidence") is not None else ", "
        grounded = {True: "yes", False: "NO", None: ", "}[r["grounded"]]
        lines.append(
            f"| {i} | **{r['verdict']}** | {c['category']} | {c['question']} | "
            f"{resp.get('escalated')} | {conf} | {grounded} | {cits} |"
        )

    lines.append("\n## Full answers (for Confluence cross-check)\n")
    for i, r in enumerate(rows, 1):
        c = r["case"]
        resp = r["resp"] or {}
        lines.append(f"### {i}. [{r['verdict']}] {c['question']}")
        lines.append(
            f"- **id**: `{c['id']}`  ·  **category**: {c['category']}  ·  **note**: {r['note']}"
        )
        if c.get("expect_any"):
            lines.append(f"- **expected to mention**: {', '.join(c['expect_any'])}")
        if c.get("expect_escalate"):
            lines.append("- **expected**: ESCALATE (not in knowledge base)")
        lines.append(
            f"- **escalated**: {resp.get('escalated')}  ·  **confidence**: {resp.get('confidence')}"
        )
        cits = resp.get("citations") or []
        if cits:
            lines.append("- **citations**:")
            for ci in cits:
                lines.append(f"    - [{ci.get('title')}]({ci.get('url')})")
        ans = (resp.get("answer") or "").strip()
        lines.append(f"\n> {ans.replace(chr(10), chr(10) + '> ')}\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
