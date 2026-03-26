FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy source
COPY pyproject.toml .
COPY src/ src/
COPY static/ static/
COPY data/ data/

# Install in editable mode so __file__ stays under /app/src (needed for static path resolution)
RUN uv pip install --system -e ".[web]"

# Persistent volume mount point for SQLite DB
RUN mkdir -p /data

EXPOSE 8000

CMD ["python", "-m", "bracket_team", "serve", "--host", "0.0.0.0", "--port", "8000"]
