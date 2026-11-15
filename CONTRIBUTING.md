# Contributing to KAI

Thanks for your interest. KAI is a self-hosted, grounded RAG assistant whose
rule is never fabricate: every confident answer is grounded in retrieved sources
and cited; anything unsupported is escalated. Keep that invariant in any change
you propose.

## Development setup

```bash
python run/setup.py install      # venv + runtime deps + .env + database
# edit .env: LLM / embeddings / DB / sources
python run/setup.py start        # run the API
python run/setup.py ingest       # build the index
```

`install` creates `.env` from `.env.example` when missing. Runtime deps only;
install the test and lint tools before running the gate:

```bash
pip install -e ".[dev,teams,uitest]"
python -m playwright install chromium   # only for the browser-driven web-UI suite
```

See [README.md](README.md) and [doc/](doc/) for architecture and configuration.

## The gate

A change is not done until this is green:

```bash
pytest
ruff check . && ruff format .
```

Integration, live-model, and web-UI suites skip unless you point them at the
services they need (`KAI_TEST_DATABASE_URL`, `KAI_TEST_LLM_BASE_URL`, Playwright);
see the README Testing section. CI runs all of them.

Run these before every PR. New behaviour needs new tests: the suite is the
contract.

## Guardrails

- Do not weaken a grounding / anti-fabrication guard (`kai/pipeline/ask.py`,
  `kai/pipeline/verify.py`) without re-running the eval and explaining the
  trade-off.
- Do not commit secrets. `.env` is git-ignored; do not add credentials to code,
  tests, or fixtures.
- Prefer escalation over a guess.

## Pull requests

- Branch from `main`, keep each PR focused on one change.
- Describe what changed, why, and how you tested it.
- Match the surrounding code style (enforced by `ruff`).
