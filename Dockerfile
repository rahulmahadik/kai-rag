# KAI production image (FastAPI under uvicorn).
#
#   docker build -t kai .
#   docker run --env-file .env -p 8100:8100 kai
#
# The default build is light (no torch). For the full-accuracy cross-encoder
# reranker, build with the rerank extra and set RERANKER=cross_encoder:
#   docker build --build-arg EXTRAS=bot,slack,rerank -t kai .
FROM python:3.14-slim

LABEL org.opencontainers.image.title="KAI" \
      org.opencontainers.image.description="Self-hosted grounded RAG knowledge assistant" \
      org.opencontainers.image.source="https://github.com/rahulmahadik/kai-rag" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KAI_HOST=0.0.0.0 \
    KAI_PORT=8100

WORKDIR /app

# Install the package + its core deps from pyproject. "bot,slack" lets the same
# image also run the Webex/Slack bot; add "rerank" to pull the cross-encoder.
ARG EXTRAS=bot,slack
COPY pyproject.toml README.md ./
COPY kai ./kai
RUN pip install --upgrade pip && pip install ".[${EXTRAS}]"

# Run as an unprivileged user: a container that only serves HTTP has no reason to
# hold root, so a code-execution bug cannot write to the image or escalate as easily.
# The HOME is needed for the reranker's model cache when RERANKER=cross_encoder.
RUN useradd --create-home --uid 10001 kai && chown -R kai:kai /app
USER kai
ENV HOME=/home/kai

EXPOSE 8100

# /health is intentionally open (no API key) so it can serve liveness/readiness.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request as u,sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8100/health').status==200 else 1)"

CMD ["sh", "-c", "uvicorn kai.app:app --host ${KAI_HOST} --port ${KAI_PORT}"]
