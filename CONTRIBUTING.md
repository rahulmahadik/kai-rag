# Contributing to KAI

Thanks for your interest! KAI is a self-hosted, grounded RAG assistant whose **#1
rule is _never fabricate_**, every confident answer is grounded in retrieved sources
and cited; anything unsupported is escalated. Please keep that invariant in mind in
any change you propose.

## Development setup

```bash
python run/setup.py install      # venv + runtime deps + .env + database
cp .env.example .env             # then edit: LLM / embeddings / DB / sources
python run/setup.py start        # run the API
python run/setup.py ingest       # build the index
```

`setup.py install` installs what the app needs to run. The test and lint tools are
a separate extra, so install those too before running the gate below:

```bash
pip install -e ".[dev,teams,uitest]"
python -m playwright install chromium   # only for the browser-driven web-UI suite
```

See [README.md](README.md) and [doc/](doc/) for architecture and configuration.

## The gate. A change isn't done until this is green

```bash
pytest                 # unit suite passes (no services needed)
ruff check . && ruff format .   # lint + format clean
```

The integration, live-model and web-UI suites are skipped unless you point them at
the services they need (`KAI_TEST_DATABASE_URL`, `KAI_TEST_LLM_BASE_URL`, Playwright);
see the README's Testing section. CI runs all of them.

Run these before every PR. **New behavior needs new tests**: the test suite is the
contract.

## Guardrails (please don't break these)

- **Never weaken** a grounding / anti-fabrication guard (`kai/pipeline/ask.py`,
  `kai/pipeline/verify.py`) without re-running the eval and explaining the trade-off.
- **Never commit secrets**, `.env` is git-ignored; don't add credentials to code,
  tests, or fixtures.
- Keep answers grounded and cited; prefer escalation over a guess.

## Pull requests

- Branch from `main`, keep each PR focused on one change.
- Describe **what** changed, **why**, and **how you tested it**.
- Match the surrounding code style (it's enforced by `ruff`).
