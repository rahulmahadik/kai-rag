# Knowledge sources

KAI ingests from one or more sources behind the `KBSource` protocol. The rest of
the pipeline (chunk → embed → store → retrieve → answer, and every grounding
guard) is **source-agnostic**, so the no-fabrication guarantee holds for any
source, Confluence, files, or a future connector.

## Switch sources with `SOURCE_TYPE`

```dotenv
SOURCE_TYPE=confluence          # default: a Confluence space
SOURCE_TYPE=files               # local files under SOURCE_DIR
SOURCE_TYPE=confluence+files    # both, ingested together (alias: "both")

SOURCE_DIR=/path/to/docs        # required when SOURCE_TYPE includes files
```

Then ingest as usual: `python run/setup.py ingest` (or `POST /ingest`).

## Multiple sources

You can ingest from several sources at once. They're combined transparently
(`CompositeKBSource`), and every chunk keeps its own source/space tag, so retrieval
and the no-fabrication guards are unchanged.

**Multiple spaces** in one Confluence instance, comma-separate `CONFLUENCE_SPACE_KEY`:

```dotenv
CONFLUENCE_SPACE_KEY=ENG,OPS,DOCS
```

**Multiple Confluence instances**: the flat `CONFLUENCE_*` vars are instance #1; add
more with numbered vars. Each instance has its own base URL, spaces, and auth, so a
Confluence **Cloud** site and a self-hosted Confluence **Server/Data Center** wiki
can be ingested together, and a **public** instance can sit alongside a **private**
one (the example below):

```dotenv
# instance #1, PUBLIC space: leave email + token blank (anonymous read)
CONFLUENCE_BASE_URL=https://cwiki.apache.org/confluence
CONFLUENCE_SPACE_KEY=COMDEV

# instance #2, PRIVATE space: set BOTH email + token (independent of instance #1)
CONFLUENCE_2_BASE_URL=https://acme.atlassian.net/wiki
CONFLUENCE_2_SPACE_KEY=ENG,OPS
CONFLUENCE_2_EMAIL=me@acme.com
CONFLUENCE_2_API_TOKEN=...                  # _ROOT_PAGE / _MAX_DOCS optional
```

### Authentication (resolved per instance)

| `EMAIL` + `API_TOKEN` | result |
| --- | --- |
| both set | HTTP **Basic**, Cloud API token, or Server/DC username+password (**private**) |
| token only (no email) | **Bearer**, Server/Data Center **Personal Access Token** |
| both blank | **anonymous** (public space) |
| email only (no token) | configuration error, **fails loudly** |

Auth is **independent per instance**: a numbered instance never inherits instance
#1's credentials, so each Confluence/wiki can use its **own key** (and even a
different auth *type*). Mixing public + private is safe in any order.

- **Cloud (private):** create a token at *id.atlassian.com → Security → API tokens*,
  then set `CONFLUENCE_<n>_EMAIL` + `CONFLUENCE_<n>_API_TOKEN`.
- **Server / Data Center wiki:** either set username + password (Basic), or set just
  `CONFLUENCE_<n>_API_TOKEN` to a Personal Access Token (Bearer, leave email blank).

**Multiple directories**, comma-separate `SOURCE_DIRS` (overrides `SOURCE_DIR`):

```dotenv
SOURCE_TYPE=files
SOURCE_DIRS=./samples,/mnt/handbook,/srv/runbooks
```

All of these compose with `SOURCE_TYPE=confluence+files`, e.g. two Confluence
instances **and** two local folders, ingested together.

> Any wiki/site that speaks the **Confluence REST API** works as an instance
> (Cloud or Server/DC). Non-Confluence wikis (MediaWiki, generic doc sites) need a
> different connector, see [the extension point](#adding-another-source-type) or
> the web-source item on the [roadmap](roadmap.md).

## Files source

`SOURCE_DIR` is walked recursively, hidden/dotfiles and junk dirs (`.git`,
`node_modules`, `dist`, build caches) are skipped, and files larger than
`FILE_MAX_BYTES` (default ~25 MB) are skipped with a log. Supported files (others skipped):

| extension | handling |
| --- | --- |
| `.pdf` | text extracted with `pypdf`, one blank line per page (`content_type=text`) |
| `.md` `.markdown` | read as-is; markdown `#`/`##` headings drive header-aware chunking |
| `.rst` | read as-is; only `#`-style headings are recognized, so RST underline headings (`====`) aren't, chunked as plain prose |
| `.txt` `.text` `.log` | read as-is (`content_type=text`) |
| `.html` `.htm` | read and cleaned like Confluence HTML (`content_type=html`) |

Each file → one `Doc` (id = relative path, so re-ingest is idempotent; url =
`file://...` so citations stay clickable). Empty/unreadable files are skipped; a
single bad file never fails the run.

Plain-text sources are **never HTML-stripped**, so prose containing `<` or `&`
survives intact (only HTML sources get tag/macro stripping).

**Unsupported drops** (Office `.docx`/`.xlsx`/`.pptx`, images, archives, media) are
skipped during a crawl; a scanned/image-only PDF with no text layer is skipped with a
`kai_pdf_no_text` warning (it would need OCR first). The ad-hoc `/ask-document` path
replies with a clear "I can't read **.docx** files" message rather than guessing.

## Adding another source type

Implement `KBSource.iter_pages() -> Iterable[Doc]` and wire it into
`kai/factory.py:_build_kb`. Set `Doc.content_type` to `"text"` for already-plain
content or `"html"` for markup. `CompositeKBSource(*sources)` combines several.

## Vector precision (`VECTOR_TYPE`)

```dotenv
VECTOR_TYPE=vector    # float32, exact (default)
VECTOR_TYPE=halfvec   # fp16, 2x smaller, measured lossless (recall@10=1.000)
```

`halfvec` only matters at large scale (millions of chunks); it changes nothing
measurable on a small corpus. Changing it requires a re-ingest.

## Bundled sample documents (`samples/`)

A few small documents ship under `samples/` so you can ingest and test KAI
immediately (`SOURCE_TYPE=files`, `SOURCE_DIR=./samples`). They're **numbered** so you
can skim them in order; together they act as a tiny fictional-company knowledge base.

| File | What it is | License |
| --- | --- | --- |
| `01_kai_overview.pdf` | one-page overview of KAI | MIT (this project) |
| `02_ledger_service_guide.md` | a fictional service guide (markdown headings) | MIT (this project) |
| `03_oncall_runbook.txt` | on-call rotation + incident-response runbook | MIT (this project) |
| `04_password_reset_faq.txt` | account / password / MFA help FAQ | MIT (this project) |
| `05_vpn_access_guide.txt` | VPN & remote-access guide | MIT (this project) |

**Every bundled sample is original content written for this repo (MIT)**, no
third-party material is redistributed. The web UI's suggestion chips ("How do I reset
my password?", "Summarize our on-call process") are answered straight from these.

To test KAI on a larger real-world PDF, download one yourself into `SOURCE_DIR`
for example the *Attention Is All You Need* paper from
<https://arxiv.org/abs/1706.03762> (not bundled, to avoid redistributing a
third-party PDF).
