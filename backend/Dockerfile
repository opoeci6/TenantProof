# Build context is the repo root so web/ can be served by the API.
FROM python:3.12-slim

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
# PyPI lookups inside the build can fail ("dns error ... Try again"); retry more than uv's
# default of 3, with a longer wait per request, instead of failing the whole build.
ENV UV_HTTP_RETRIES=10 UV_HTTP_TIMEOUT=60

# uv from PyPI (the glibc build), not the static musl binary in the uv image. Some Docker setups,
# such as a Mac VM whose DNS forwarder never answers AAAA (IPv6) queries, make every lookup wait
# for an IPv6 timeout, and with 50 parallel downloads that fails the build. glibc can skip IPv6
# lookups (RES_OPTIONS=no-aaaa); the musl binary has no such switch. The option is set on the
# build steps only; nothing changes in the running image.
RUN RES_OPTIONS=no-aaaa pip install --no-cache-dir "uv>=0.11,<0.12"
# PyPI lookups inside the build can fail now and then ("dns error ... Try again"); retry more
# than uv's default of 3, with a longer wait per request, instead of failing the whole build.
ENV UV_HTTP_RETRIES=10 UV_HTTP_TIMEOUT=60

WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN RES_OPTIONS=no-aaaa uv sync --frozen --no-dev --no-install-project

COPY backend/ ./
COPY web/ /app/web/

RUN useradd --create-home app && mkdir -p /data/storage && chown -R app:app /app /data
USER app
EXPOSE 8000
CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
