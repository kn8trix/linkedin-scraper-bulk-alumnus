FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Writable scratch for the cookie jar and the decoration pin. The jar lets the
# process ride an li_at rotation within a machine's lifetime; on restart it
# falls back to the credential in the environment, which is the correct
# behaviour anyway. Mount a volume here if you want it to survive restarts.
ENV LI_STATE_DIR=/tmp/state
RUN mkdir -p /tmp/state

EXPOSE 8080

# --workers 1 is load-bearing, not a default. The cache, the circuit breaker and
# the single-flight locks are per-process, and there is exactly ONE upstream
# LinkedIn session. A second worker would double the upstream call rate and
# break the breaker.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
