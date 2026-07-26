"""Provider wiring for KAI.

``build_providers`` is the single place that decides which concrete
implementation of each provider Protocol the application uses, constructed from
:class:`~kai.config.Settings`. Every provider is REAL (network/DB-backed):

* embedder, LLM, vector store and Confluence knowledge source are REQUIRED, a
  blank config raises loudly rather than silently degrading;
* the escalation tracker is real Jira when configured, otherwise a local tracker
  that records escalations without opening an external ticket.

Provider modules are imported inside the function so only the SDKs you actually
use get loaded.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from kai.config import Settings
from kai.interfaces import Embedder, KBSource, LLMClient, Tracker, VectorStore

logger = logging.getLogger("kai.factory")

# The concrete return tuple, named for readability at call sites.
Providers = tuple[Embedder, LLMClient, VectorStore, KBSource, Tracker]


def _set(*vals: str) -> bool:
    return all(bool(v and v.strip()) for v in vals)


def _require(ok: bool, what: str, *envs: str) -> None:
    if not ok:
        raise ValueError(f"{what} is not configured. Set {', '.join(envs)} in your .env.")


def _effective_env(settings: Settings) -> dict[str, str]:
    """Merge the ``.env`` file (which pydantic reads but does NOT export to
    ``os.environ``) with the process environment, so numbered multi-instance keys
    (``CONFLUENCE_2_BASE_URL`` ...) are visible whether set in ``.env`` (local dev) or
    the shell/container (Docker). ``os.environ`` wins on conflict."""

    merged: dict[str, str] = {}
    env_file = settings.model_config.get("env_file") or ".env"
    try:
        path = Path(str(env_file)).expanduser()
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                # mirror dotenv: drop an unquoted inline "# comment", strip quotes
                value = value.split(" #", 1)[0].strip().strip('"').strip("'")
                if key.strip():
                    merged[key.strip()] = value
    except OSError:
        pass
    merged.update(os.environ)
    return merged


def _confluence_instances(settings: Settings) -> list[Settings]:
    """Every configured Confluence instance, each as a ``Settings`` carrying its own
    ``confluence_*`` values. The flat ``CONFLUENCE_*`` vars are instance 0 (back-
    compat); additional instances are numbered ``CONFLUENCE_<n>_BASE_URL`` plus
    matching ``_SPACE_KEY`` / ``_EMAIL`` / ``_API_TOKEN`` / ``_ROOT_PAGE`` /
    ``_MAX_DOCS``.

    Auth is INDEPENDENT per instance: a numbered instance never inherits the flat
    instance's email/token. So you can freely mix a private site (its own
    email+token) with a public/anonymous one (neither), and a half-configured
    instance (only one of the two) still fails loudly. Only ``_MAX_DOCS`` falls back
    to the flat default (it's a crawl-size knob, not a credential)."""

    instances: list[Settings] = []
    if settings.confluence_base_url.strip():
        instances.append(settings)

    env = _effective_env(settings)
    indices = sorted(
        {
            int(m.group(1))
            for key in env
            for m in [re.match(r"^CONFLUENCE_(\d+)_BASE_URL$", key)]
            if m
        }
    )
    for i in indices:
        base = (env.get(f"CONFLUENCE_{i}_BASE_URL") or "").strip()
        if not base:
            continue
        instances.append(
            settings.model_copy(
                update={
                    "confluence_base_url": base,
                    "confluence_space_key": (env.get(f"CONFLUENCE_{i}_SPACE_KEY") or "").strip(),
                    # Auth is per-instance: NEVER inherit the flat instance's
                    # credentials (so a private #1 + public #2 mix stays correct).
                    "confluence_email": (env.get(f"CONFLUENCE_{i}_EMAIL") or "").strip(),
                    "confluence_api_token": (env.get(f"CONFLUENCE_{i}_API_TOKEN") or "").strip(),
                    "confluence_root_page": (env.get(f"CONFLUENCE_{i}_ROOT_PAGE") or "").strip(),
                    "confluence_max_docs": int(
                        env.get(f"CONFLUENCE_{i}_MAX_DOCS") or settings.confluence_max_docs or 0
                    ),
                }
            )
        )
    # Numbered keys present without a base URL (typo / mis-numbered block), warn so a
    # block like CONFLUENCE_12_SPACE_KEY meant for #2 isn't silently dropped.
    incomplete = {
        int(m.group(1))
        for key in env
        for m in [
            re.match(r"^CONFLUENCE_(\d+)_(?:SPACE_KEY|EMAIL|API_TOKEN|ROOT_PAGE|MAX_DOCS)$", key)
        ]
        if m
    } - set(indices)
    for i in sorted(incomplete):
        logger.warning(
            "kai_confluence_instance_incomplete index=%d, CONFLUENCE_%d_* set but no "
            "CONFLUENCE_%d_BASE_URL; that instance is ignored.",
            i,
            i,
            i,
        )
    if instances:
        # strip any inline userinfo (https://user:pass@host) before logging
        safe = [re.sub(r"//[^/@]*@", "//", s.confluence_base_url) for s in instances]
        logger.info("kai_confluence_instances count=%d urls=%s", len(instances), safe)
    return instances


def _file_dirs(settings: Settings) -> list[str]:
    """Directories to ingest: ``SOURCE_DIRS`` (comma-separated) if set, else the
    single ``SOURCE_DIR`` (back-compat)."""

    raw = (getattr(settings, "source_dirs", "") or "").strip() or (
        settings.source_dir or ""
    ).strip()
    # de-dup while preserving order so a repeated dir isn't crawled/embedded twice
    return list(dict.fromkeys(d.strip() for d in raw.split(",") if d.strip()))


def _build_kb(settings: Settings) -> KBSource:
    """Build the knowledge source(s) from ``SOURCE_TYPE`` (fail loud on blanks).

    ``confluence`` (default), ``files`` (local PDF/md/txt/html), or both
    (``confluence+files`` / ``both``). Each type can have MULTIPLE sources, several
    Confluence instances (numbered ``CONFLUENCE_<n>_*``) and/or several directories
    (``SOURCE_DIRS``), all combined via ``CompositeKBSource``.
    """

    st = (settings.source_type or "confluence").strip().lower()
    # Tokenize on any common separator so order/spacing/aliases all work
    # (confluence+files, files,confluence, "files & confluence", file, etc.).
    tokens = {t for t in re.split(r"[+,/&|\s]+", st) if t}
    both = bool(tokens & {"both", "all"})
    want_conf = both or "confluence" in tokens
    want_files = both or bool(tokens & {"files", "file"})
    if not (want_conf or want_files):
        raise ValueError(
            f"Unsupported SOURCE_TYPE {settings.source_type!r}; use "
            "'confluence', 'files', or 'confluence+files'."
        )

    sources: list[KBSource] = []
    if want_conf:
        from kai.providers.confluence_cloud import ConfluenceCloudKBSource

        instances = _confluence_instances(settings)
        if not instances:
            raise ValueError(
                "Confluence knowledge source is not configured. Set CONFLUENCE_BASE_URL "
                "+ CONFLUENCE_SPACE_KEY (or CONFLUENCE_<n>_BASE_URL for several instances) "
                "in your .env."
            )
        # base_url + space_key are enough, auth is optional (anonymous public spaces).
        # Each instance's CONFLUENCE_SPACE_KEY may be comma-separated (multi-space):
        # one connector per (instance, space); chunks keep their own space tag, so
        # retrieval needs no change.
        for inst in instances:
            _require(
                _set(inst.confluence_base_url, inst.confluence_space_key),
                "Confluence knowledge source",
                "CONFLUENCE_BASE_URL",
                "CONFLUENCE_SPACE_KEY",
            )
            space_keys = [k.strip() for k in inst.confluence_space_key.split(",") if k.strip()]
            for key in space_keys:
                per_space = (
                    inst
                    if len(space_keys) == 1
                    else inst.model_copy(update={"confluence_space_key": key})
                )
                sources.append(ConfluenceCloudKBSource(per_space))

    if want_files:
        dirs = _file_dirs(settings)
        _require(bool(dirs), "File knowledge source", "SOURCE_DIR")
        from kai.providers.file_source import FileKBSource

        single = (settings.source_dir or "").strip()
        for d in dirs:
            per_dir = (
                settings
                if d == single and not (getattr(settings, "source_dirs", "") or "").strip()
                else settings.model_copy(update={"source_dir": d})
            )
            sources.append(FileKBSource(per_dir))

    if len(sources) == 1:
        return sources[0]
    from kai.providers.file_source import CompositeKBSource

    return CompositeKBSource(*sources)


def build_providers(settings: Settings) -> Providers:
    """Construct the five real providers from ``settings`` (fail loud on blanks).

    Returns ``(embedder, llm, store, kb, tracker)``.
    """

    _require(
        _set(settings.embed_base_url, settings.embed_model),
        "Embedder",
        "EMBED_BASE_URL",
        "EMBED_MODEL",
    )
    from kai.providers.embedding_openai import OpenAIEmbedder

    embedder: Embedder = OpenAIEmbedder.from_settings(settings)

    _require(
        _set(settings.llm_base_url, settings.llm_model),
        "LLM",
        "LLM_BASE_URL",
        "LLM_MODEL",
    )
    from kai.providers.llm_openai import OpenAILLM

    llm: LLMClient = OpenAILLM.from_settings(settings)

    _require(_set(settings.database_url), "Vector store", "DATABASE_URL")
    from kai.providers.vectorstore_pgvector import PgVectorStore

    store: VectorStore = PgVectorStore(
        database_url=settings.database_url,
        table=settings.vector_table,
        vector_type=getattr(settings, "vector_type", "vector"),
    )

    kb: KBSource = _build_kb(settings)

    # Escalation tracker: real Jira when fully configured, else a local tracker
    # that records the escalation without a fake ticket URL.
    if _set(
        settings.jira_base_url,
        settings.jira_email,
        settings.jira_api_token,
        settings.jira_project_key,
    ):
        from kai.providers.jira_cloud import JiraCloudTracker

        tracker: Tracker = JiraCloudTracker(settings)
    else:
        from kai.providers.local_tracker import LocalTracker

        tracker = LocalTracker()

    return embedder, llm, store, kb, tracker
