FROM python:3.14-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY config ./config
COPY prompts ./prompts
ENV PYTHONUNBUFFERED=1 CONFIG_DIR=/app/config PROMPTS_DIR=/app/prompts
