FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY migrations ./migrations
RUN pip install --no-cache-dir .
ENV GSM_ENV=development
EXPOSE 8080
CMD ["uvicorn", "gsm.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
