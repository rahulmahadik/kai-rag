# KAI web frontend

A tiny, dependency-free web chat UI for KAI. It is a **separate** static app — it
does not run inside the backend; it just calls the KAI HTTP API (`POST /ask`, and
`POST /ask-document` when you attach a file) over CORS. One file, no build step, no
framework. Attaching a file (📎) runs ad-hoc Q&A over **that file only** — it is
read for that one question and never saved to the knowledge base.

## Run it

The API must be running first (default `http://localhost:8100`). Then serve this
folder with any static server — **whatever origin you serve from must be listed in the
backend's `CORS_ORIGINS`** (the shipped `.env.example` pre-allows `:3000` and `:5173`):

```bash
python run/setup.py ui                 # serves frontend/ on http://localhost:3000
# or, from the repo root:
python -m http.server 5173 -d frontend # http://localhost:5173
# or with Node (defaults to http://localhost:3000):
npx serve frontend
```

Opening `frontend/index.html` directly (a `file://` URL) also works for a quick look,
but set the API base in the header field since there's no same-origin default.

## Point it at the API

The header has an **API base URL** field (default `http://localhost:8100`) and an
optional **API key** field (fill it if `KAI_API_KEY` is set on the backend). Both
are remembered in the browser.

Because the frontend and backend are separate origins, the backend must allow the
frontend's origin via CORS — set `CORS_ORIGINS` in the backend `.env` to the UI's exact
origin (e.g. `http://localhost:3000`). It is **deny-by-default**: when unset, NO
cross-origin browser access is granted, so the UI can't reach the API until you set it.
(`*` works but warns loudly — never use it with an internet-exposed, unauthenticated API.)

## Docker

`docker compose up` serves this folder via nginx on <http://localhost:3000> and the
API on `:8100`. The stack loads the backend's `.env` (copy it from `.env.example`
first), which already lists `http://localhost:3000` in `CORS_ORIGINS`, so the
Dockerized UI can reach the API out of the box.
