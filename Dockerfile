# One image for the API, the analysis worker and the sync CLI.
#
# What is deliberately NOT in it: the Stockfish binary. The installer picks
# the fastest SIMD build *this CPU* can run by actually running it, and an
# image is copied to machines with different CPUs - a build picked on the
# build machine can die with SIGILL on the host. So the installer runs in the
# entrypoint, on the machine that will execute the engine, into a named volume
# so the download happens once per machine. See INTERVIEW_PREP.md.

FROM python:3.12-slim-bookworm

# Pinned uv (the version the lock file is maintained with), copied from its
# official image rather than pip-installed.
COPY --from=ghcr.io/astral-sh/uv:0.5.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, in their own layer: they change far less often than
# the source, so a code change does not re-download them.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY tools ./tools
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /app/vendor \
    && chown app:app /app/vendor
USER app

# tools/fetch_stockfish.py installs into /app/vendor/stockfish/.
ENV STOCKFISH_PATH=/app/vendor/stockfish/stockfish

ENTRYPOINT ["entrypoint.sh"]
CMD ["chess-coach-worker"]
