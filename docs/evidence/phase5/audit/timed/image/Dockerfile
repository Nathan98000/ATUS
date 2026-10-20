# THROWAWAY prototype API image for the Phase 5 free-tier-fit audit (not a production Dockerfile).
# Build context: a `git archive 387fc2d pyproject.toml README.md LICENSE src` export (./src here).
FROM python:3.12-slim
# procps only so `ps` can report RSS inside the container during the audit.
RUN apt-get update && apt-get install -y --no-install-recommends procps && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY src/ /app/
# Non-editable install of the repository package (runtime deps only, no [dev]).
RUN pip install --no-cache-dir /app && pip freeze > /pip-freeze.txt && rm -rf /app/src
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=0
EXPOSE 8000
# One uvicorn process, no reload, no extra workers (uvicorn default = 1 worker, in-process).
CMD ["uvicorn", "--factory", "atus_pipeline.api.app:create_app", "--host", "0.0.0.0", "--port", "8000"]
