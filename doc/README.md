# KAI documentation index

| Doc | What |
| --- | --- |
| [api.md](api.md) | Every endpoint, and how a question flows to an answer |
| [setup-and-run.md](setup-and-run.md) | Install, configure `.env`, ingest, run the API |
| [deploy.md](deploy.md) | Docker Compose or systemd, provider matrix, prod checklist |
| [integrations-setup.md](integrations-setup.md) | Webex, Slack, and Teams: create the bot, tokens, run |
| [chat-platforms.md](chat-platforms.md) | Chat-adapter architecture and adding a platform |
| [sources.md](sources.md) | Confluence, local files/PDF, URL crawl; `SOURCE_TYPE`, space ACL |
| [architecture.md](architecture.md) | Pipeline, guards, providers |
| [faq.md](faq.md) | Setup, config, accuracy, bots, gotchas |
| [roadmap.md](roadmap.md) | Done and planned |
| [requirements.md](requirements.md) | Design decisions |
| [../CHANGELOG.md](../CHANGELOG.md) | Release history |

## Quick start

```bash
python run/setup.py install        # venv + deps + .env (from .env.example) + DB
# edit .env  (LLM / DB / sources)
python run/setup.py start          # API on :8100
python run/setup.py ingest         # build the index
python run/setup.py ui             # web chat UI on :3000
```

`install` creates `.env` when missing; only copy `.env.example` yourself if you
need a fresh file. For a long-running demo, `python run/setup.py supervise`
keeps the API alive with auto-restart. Chat bots:
`CHAT_PLATFORM=webex|slack|teams python run/setup.py bot`.

Validation: `pytest` (unit suite, no services needed). Integration, live-LLM,
and web-UI suites under `tests/integration/` skip unless you set
`KAI_TEST_DATABASE_URL` / `KAI_TEST_LLM_BASE_URL` or install Playwright. The
live accuracy gate is `python eval/run_eval.py` (needs a real LLM).

## Evaluation

`eval/` holds the accuracy harnesses: `golden.json` / `golden_bare.json`,
`run_eval.py`, `validate_space.py`, `inspect_quality.py`, and `simulate_bot.py`
(drive the bot logic without platform tokens).
