FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends git build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml MANIFEST.in README.md LICENSE ./
COPY . .

RUN pip install --upgrade pip build \
 && pip install ".[test]"

RUN pytest -q

RUN python -m build --wheel --sdist --outdir /dist

FROM python:3.12-slim AS dist
COPY --from=builder /dist /dist
CMD ["ls", "-la", "/dist"]

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY --from=builder /dist /tmp/dist
RUN pip install /tmp/dist/*.whl && rm -rf /tmp/dist

ENTRYPOINT ["telos"]
CMD ["--help"]
