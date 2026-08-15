FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependency layer, cached separately from source.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# backtest.py and verify_feeds.py are here for `fly ssh console`: --replay is
# the only way to recover a send that failed, and a dead feed is silent
# without verify_feeds. dashboard.py is not a console tool - main.py imports it
# to serve the corpus on DASHBOARD_PORT, so it must be in the image.
COPY main.py config.py dashboard.py backtest.py verify_feeds.py ./
RUN uv sync --frozen --no-dev

EXPOSE 8080

# The venv interpreter directly, not `uv run`. `uv run` re-resolves the
# lockfile at container start - it was downloading ruff (10MB, dev group) on
# every boot, which is 15s of nothing and turns a package-index blip into a
# crash loop.
CMD ["/app/.venv/bin/python", "main.py"]
