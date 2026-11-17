FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-cache

COPY src ./src
COPY config.json.sample ./config.json
RUN mkdir -p /app/books /app/data

EXPOSE 5000
CMD ["flask", "--app", "src.main", "run", "--host=0.0.0.0", "--port=5000", "--no-debugger", "--no-reload"]
