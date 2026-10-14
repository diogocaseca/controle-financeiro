FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY controle_financeiro ./controle_financeiro
RUN uv sync --frozen --no-dev
ENV CF_DATA_DIR=/data CF_HOST=0.0.0.0 CF_PORT=8000
VOLUME /data
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "controle-financeiro"]
