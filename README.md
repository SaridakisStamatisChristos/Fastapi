# FastAPI task service

Local single-node demonstration, not a public multi-tenant service.

```sh
python -m pip install -r requirements-dev.txt
python -m uvicorn app:app --host 127.0.0.1
```

Open http://127.0.0.1:8000/docs. TASK_DB sets the SQLite path (default tasks.sqlite3).

POST /tasks creates a task; GET /tasks lists by ascending ID with limit 1–100 and offset 0–2147483647. GET, PUT and DELETE /tasks/{id} retrieve, replace and remove. Titles are trimmed, 1–200 characters; done is a strict boolean defaulting to false. Unknown fields are rejected. Missing tasks return 404, invalid requests 422, database errors 503. POST is not idempotent. PUT replaces the full task. Health checks process response; readiness checks database reads, not write availability. SQLite transactions serialize writes; no distributed guarantees. Bind only to trusted local interfaces; no authentication.

Run tests: `python -m pytest -q --cov=app --cov-branch --cov-fail-under=95`.
