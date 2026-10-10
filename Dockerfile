FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
# Keep-alive longer than the load balancer's idle timeout (AWS ALB: 600 s), or the balancer can reuse a connection
# the server just closed and the shopper gets a random 502. Proxy headers give the shopper's real address.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 620 --proxy-headers --forwarded-allow-ips '*'"]

