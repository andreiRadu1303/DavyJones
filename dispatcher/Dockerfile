FROM python:3.11-slim

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Allow git to work with vault owned by host user
RUN git config --global --add safe.directory /vault

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# SRC_HASH is passed by 'davyjones start' as the git commit hash of
# dispatcher/src/. When any source file changes and is committed, the
# hash changes and Docker rebuilds only this layer, keeping apt/pip cached.
ARG SRC_HASH=unknown
COPY src/ ./src/

CMD ["python", "-m", "src.main"]
