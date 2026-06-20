# Deploying KAI

KAI is a small FastAPI service plus a Postgres database (with the `pgvector`
extension). You point it at an OpenAI-compatible **LLM** endpoint and an
OpenAI-compatible **embeddings** endpoint, ingest your knowledge sources once,
and it serves grounded answers over an HTTP API, a web chat UI, and (optionally)
a Webex/Slack bot.

This guide covers two paths: **Docker Compose** (turnkey) and **bare-metal /
VM** (venv + systemd).

---

## What you need

| Component        | Requirement |
|------------------|-------------|
| Platform         | Linux or macOS (tested); Windows via WSL or Docker |
| Python           | 3.12+ (only for the bare-metal path) |
| Database         | Postgres 14+ with `pgvector` (the `pgvector/pgvector` image bundles it) |
| Embeddings model | Any OpenAI-compatible `/embeddings` endpoint |
| LLM (chat) model | Any OpenAI-compatible `/chat/completions` endpoint |

KAI creates its own table **and** the `vector` extension automatically on the
first ingest — no manual schema step (the DB user just needs `CREATE EXTENSION`
rights, which the bundled Postgres image has).

---

## Models & providers — offline, online, or mixed

KAI is **not tied to any one provider**. Both the LLM and the embedder are thin
clients over the OpenAI REST shape, selected entirely by config:

```ini
EMBED_BASE_URL=...   EMBED_MODEL=...   EMBED_API_KEY=...
LLM_BASE_URL=...     LLM_MODEL=...     LLM_API_KEY=...
```

So you can run it:

KAI's client speaks plain OpenAI REST: it POSTs to `{base_url}/chat/completions`
and `{base_url}/embeddings` with an `Authorization: Bearer <key>` header and the
model name in the body. **Anything that matches that shape works by config; things
that don't, don't** — so the honest list is:

| Mode | Works by config | Notes |
|------|-----------------|-------|
| **Fully offline / self-hosted** | Any OpenAI-compatible local server — e.g. Ollama, vLLM, llama.cpp, LM Studio, LocalAI | Your data never leaves your network. The validated default is Ollama (`qwen2.5:14b-instruct` + `nomic-embed-text`). |
| **Online / hosted** | Any OpenAI-shaped hosted API — e.g. OpenAI, Together, Fireworks, DeepInfra | Needs both `/chat/completions` **and** `/embeddings` over Bearer auth. Set `LLM_*` / `EMBED_*` base URL + key + model. |
| **Mixed** | local embeddings + hosted LLM (or vice-versa) | `LLM_BASE_URL` and `EMBED_BASE_URL` are independent — same endpoint or two different ones. |

It's the **shape** that matters, not the brand: if an endpoint accepts an
`Authorization: Bearer` header and serves OpenAI-style `/chat/completions` (and, for
the embedder, `/embeddings`), it works by config — no code. The named providers are
just examples. (Chat-only hosts like Groq / OpenRouter can be the LLM but not the
embedder — no embeddings endpoint.)

> Endpoints that aren't OpenAI-shaped (a different auth header or URL scheme) aren't
> supported as-is — they'd need a small provider adapter, so they're left out.

Two rules of thumb:
- Swapping the **chat** model is free.
- Swapping the **embedding** model changes vector dimensions, so it requires a
  **re-ingest** (it rebuilds the index).

**Reranker:** the *code* default (`kai/config.py`) is `RERANKER=noop` (light, no
torch), but the shipped `.env.example` enables the full-accuracy pipeline
(`RERANKER=cross_encoder`, `MULTI_QUERY=true`) and `run/setup.py install` installs
`sentence-transformers` — so a default setup runs *with* the reranker. The
`cross_encoder` path needs the `rerank` extra (PyTorch + sentence-transformers,
~2 GB); set `RERANKER=noop` to skip it. See the build notes below.

---

## Quickest — `run/setup.py` (local laptop *or* any host)

The simplest and **most-tested** path — identical on a dev laptop and a server (it's
what the live server and the test suite exercise). Needs Python 3.12+, a reachable
Postgres, and your LLM/embedding endpoint (e.g. Ollama with the models pulled).

```bash
python run/setup.py install      # venv + deps + creates .env + ensures the DB
# edit .env (LLM_* / EMBED_* / DATABASE_URL / sources); if using Ollama, pull models:
#   ollama pull qwen2.5:14b-instruct && ollama pull nomic-embed-text
python run/setup.py start         # API on :8100 (background)
python run/setup.py ingest        # build the index from your configured sources
python run/setup.py ui            # web chat UI on :3000
python run/setup.py doctor        # verify Postgres / pgvector / Ollama / deps
```

`status` / `stop` / `restart` / `supervise` (auto-restart) manage the process;
`reindex` rebuilds the vector index in place (keeping curated answers/feedback); and
`fresh` does a **clean-slate** bring-up that **WIPES the DB** (reset-db) before
install + start + ingest — use it only on a new machine, not to refresh live data.
This is the same flow whether you're
on your laptop or SSH'd into a server — for boot persistence on a server, wrap it in
your process manager or use the systemd unit (Option B).

---

## Option A — Docker Compose (turnkey)

Brings up the API + a pgvector Postgres + the web frontend in one command. The
frontend (`frontend/`) is a separate service (nginx) that calls the API over CORS.

> **Note on the Docker path.** The Compose file and Dockerfile are provided and
> config-valid, but the most-exercised path — and what the automated test suite uses —
> is **Option B** below (`run/setup.py` / bare metal). Both are supported; if you hit a
> Docker-specific snag, please file an issue.

```bash
cp .env.example .env          # then edit: EMBED_*/LLM_* endpoints + models
docker compose up -d --build  # builds the image, starts db + kai + frontend
curl -X POST http://localhost:8100/ingest   # seed the knowledge base
open http://localhost:3000/                 # web chat UI (frontend); API is :8100
```

- The compose file overrides `DATABASE_URL` to point at the bundled `db`
  service, so you don't set it in `.env`.
- **Full-accuracy reranker:** edit `docker-compose.yml` → `args.EXTRAS:
  bot,slack,rerank` and set `RERANKER=cross_encoder` in `.env`, then
  `docker compose up -d --build`.
- **Run the models locally too:** uncomment the `ollama` service (and its
  volume), then set `EMBED_BASE_URL`/`LLM_BASE_URL` to `http://ollama:11434/v1`.
- **Chat bot:** uncomment the `bot` service.
- **Frontend / CORS:** the `frontend` service serves `frontend/` on `:3000` and
  calls the API at `:8100`. CORS is **deny-by-default** — set `CORS_ORIGINS` in
  `.env` to your frontend URL(s) (the example ships `http://localhost:3000,...`).
  To run the frontend without Docker: `python -m http.server 3000 -d frontend`.

Logs / lifecycle: `docker compose logs -f kai` · `docker compose down` ·
`docker compose down -v` (also wipes the DB volume).

---

## Option B — systemd service (Linux host)

The venv + uvicorn core below is the **same process** the Quickest path runs (so
it's exercised). The **systemd unit is a standard template — adjust the paths and
verify on your host**; it hasn't been run end-to-end in this build. If you don't
need boot persistence, the Quickest path above is enough.

```bash
python -m venv .venv && . .venv/bin/activate
pip install ".[bot,slack]"        # add ,rerank for the cross-encoder
cp .env.example .env              # edit endpoints, models, DATABASE_URL
```

Point `DATABASE_URL` at your Postgres, e.g.
`postgresql://kai:secret@localhost:5432/kai`, then run it as a managed service:

```bash
sudo cp deploy/kai.service /etc/systemd/system/kai.service
# edit User / WorkingDirectory / venv path inside the file to match your install
sudo systemctl daemon-reload && sudo systemctl enable --now kai
journalctl -u kai -f
```

---

## First run — ingest your sources

KAI answers only from what it has ingested. After the service is up:

```bash
curl -X POST http://localhost:8100/ingest
```

Configure sources in `.env`:
- **Confluence** — `CONFLUENCE_BASE_URL`, `CONFLUENCE_SPACE_KEY` (comma-separated
  for multiple spaces), token.
- **Local files / PDFs** — `SOURCE_DIR` (e.g. the bundled `samples/`).

Re-running `/ingest` is incremental (unchanged docs are skipped via a content
hash), so it's safe to schedule on a cron / timer to keep the index fresh.

---

## Chat bot (optional)

The bot is a **separate process** that POSTs to the API — it does not replace
it. Set `CHAT_PLATFORM` (`webex` | `slack`), the platform tokens, and
`KAI_API_URL` (where the API is reachable, default `http://127.0.0.1:8100`),
then run:

```bash
python -c "from kai.bot import run_bot; run_bot()"
```

Token/setup details: [integrations-setup.md](integrations-setup.md).

---

## Reverse proxy + TLS

Terminate TLS at nginx/Caddy and proxy to `:8100`. Minimal nginx:

```nginx
server {
    listen 443 ssl;
    server_name kai.example.com;
    # ssl_certificate / ssl_certificate_key ...
    location / {
        proxy_pass http://127.0.0.1:8100;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

---

## Production checklist

- [ ] **Set `KAI_API_KEY`** — protects every route except `/health`. Callers
      send `Authorization: Bearer <key>`; the web UI has a field for it. Setting a key
      also hides the interactive docs (`/docs`, `/redoc`, `/openapi.json`) so scanners
      can't enumerate the `/admin/*` surface.
- [ ] **Set `CORS_ORIGINS`** — deny-by-default; list the web UI's exact origin(s).
- [ ] **Secrets** stay in `.env` (git-ignored) or your secret manager — never
      commit real tokens.
- [ ] **Back up Postgres** — it holds the vector index and the curated/learned
      answers (the Inform loop).
- [ ] **Schedule `/ingest`** (cron / timer) to keep the index current.
- [ ] **Scale**: run uvicorn with `--workers N` behind the proxy; flip vectors
      to `halfvec` (`VECTOR_TYPE=halfvec`) for ~2× smaller storage at scale.
- [ ] **Monitor**: scrape `GET /metrics` and review `GET /admin/gaps` for
      unanswered questions.
- [ ] **Health**: `GET /health` is open and returns `200` when ready — wire it
      to your orchestrator/load balancer.

See also: [setup-and-run.md](setup-and-run.md) · [architecture.md](architecture.md) · [chat-platforms.md](chat-platforms.md).
