# Contributing to KAI

Thanks for your interest! KAI is a self-hosted, grounded RAG assistant whose **#1
rule is _never fabricate_** — every confident answer is grounded in retrieved sources
and cited; anything unsupported is escalated. Please keep that invariant in mind in
any change you propose.

## Development setup

```bash
python run/setup.py install      # venv + deps + .env + database
cp .env.example .env             # then edit: LLM / embeddings / DB / sources
python run/setup.py start        # run the API
python run/setup.py ingest       # build the index
```

See [README.md](README.md) and [doc/](doc/) for architecture and configuration.

## The gate — a change isn't done until this is green

```bash
pytest                 # full suite passes
ruff check . && ruff format .   # lint + format clean
```

Run these before every PR. **New behavior needs new tests** — the test suite is the
contract.

## Guardrails (please don't break these)

- **Never weaken** a grounding / anti-fabrication guard (`kai/pipeline/ask.py`,
  `kai/pipeline/verify.py`) without re-running the eval and explaining the trade-off.
- **Never commit secrets** — `.env` is git-ignored; don't add credentials to code,
  tests, or fixtures.
- Keep answers grounded and cited; prefer escalation over a guess.

## Pull requests

- Branch from `main`, keep each PR focused on one change.
- Describe **what** changed, **why**, and **how you tested it**.
- Match the surrounding code style (it's enforced by `ruff`).
