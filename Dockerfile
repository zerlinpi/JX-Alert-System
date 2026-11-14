FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts/export_daily_snapshot.py ./scripts/export_daily_snapshot.py
RUN pip install --no-cache-dir .
RUN useradd --create-home --uid 10001 appuser && mkdir -p /app/data/raw /app/data/reports && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000 8001
CMD ["uvicorn", "jx_alert.api:app", "--host", "0.0.0.0", "--port", "8000"]
