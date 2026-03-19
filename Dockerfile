# ===== Stage 1: Build =====
FROM ghcr.io/astral-sh/uv:0.6-python3.10-bookworm-slim AS builder

WORKDIR /app

# PyAudio のビルドに portaudio19-dev が必要
RUN apt-get update && apt-get install -y --no-install-recommends \
    portaudio19-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

# 依存関係を先にインストール (キャッシュ活用)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# アプリケーションコードと設定をコピーしてプロジェクトをインストール
COPY ssl_auto_streamer/ ssl_auto_streamer/
COPY config/ config/
COPY README.md LICENSE ./
RUN uv sync --frozen --no-dev

# ===== Stage 2: Runtime =====
FROM python:3.10-slim-bookworm

WORKDIR /app

# ランタイムに必要なのは libportaudio2 のみ
RUN apt-get update && apt-get install -y --no-install-recommends \
    libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

# ビルドステージから仮想環境と設定をコピー
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/config /app/config

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

ENTRYPOINT ["ssl-auto-streamer"]
CMD ["--config", "config/config.yaml"]
