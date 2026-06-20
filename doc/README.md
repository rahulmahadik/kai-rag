# KAI documentation index

| Doc | What |
| --- | --- |
| [api.md](api.md) | **Every endpoint, what each is for, and how a question flows (chat/API → answer)** |
| [setup-and-run.md](setup-and-run.md) | Install, configure `.env`, ingest, run the API |
| [deploy.md](deploy.md) | **Deploy: Docker Compose or systemd, provider matrix (offline/online), prod checklist** |
| [integrations-setup.md](integrations-setup.md) | **Chat bots: Webex/Slack/Teams — create the bot, get tokens, configure, run** |
| [chat-platforms.md](chat-platforms.md) | Chat-adapter architecture + how to add a platform + Webex enhancements |
| [sources.md](sources.md) | Knowledge sources: Confluence + local files/PDF; `SOURCE_TYPE`, `VECTOR_TYPE` |
| [architecture.md](architecture.md) | System architecture (pipeline, guards, providers) |
| [requirements.md](requirements.md) | Product requirements |
| [../CHANGELOG.md](../CHANGELOG.md) | Everything in the current build |

## Quick start
```bash
python run/setup.py install        # venv + deps + DB
cp .env.example .env               # then set Confluence/LLM/etc.
python run/setup.py supervise &    # API (auto-restart)
python run/setup.py ingest         # build the index
python run/setup.py bot            # chat bot (CHAT_PLATFORM selects webex|slack)
```
Validation: `pytest` (unit/integration) · `python eval/run_eval.py` (live 61-Q accuracy gate, needs Ollama).

## Evaluation
`eval/` holds the accuracy harnesses: `golden.json` / `golden_bare.json` (graded
question sets), `run_eval.py` (the 61-Q accuracy gate), `validate_space.py`,
`inspect_quality.py`, and `simulate_bot.py` (drive the bot logic without tokens).
