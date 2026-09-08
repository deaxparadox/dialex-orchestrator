# Dialex orchestrator

Dialex's orchestration service — FastAPI + Temporal + LangGraph, everything real-time/in-motion (consultations, debates). Django owns data-at-rest; this owns durable workflows, live streaming, and the LLM calls themselves.

Split out of the [Dialex monorepo](https://github.com/deaxparadox/Dialex) (`orchestrator/`) with history preserved. Full design history (ADRs, specs, API/FLOWS docs) stays in that repo — this one is code only.

## Running (standalone, without docker-compose)

Needs a running Postgres, Redis, and Temporal server (see the Dialex monorepo's docker-compose for the full stack — this repo doesn't carry that config).

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # JWT_SIGNING_KEY must match Django's SIMPLE_JWT_SIGNING_KEY exactly
uvicorn app.main:app --reload --port 8001
# separately, the Temporal worker:
python -m app.worker
```

- Health check: `http://localhost:8010/health`
- Authenticated example: `http://localhost:8010/api/me` (needs a Django-issued access token)

## Regenerating `generated_tables.py`

Whenever Django's migrations change the schema:

```
pip install -r requirements-dev.txt
sqlacodegen --generator tables "$DATABASE_URL" > app/core/generated_tables.py
```

Never hand-edit this file — see the header comment inside it.
