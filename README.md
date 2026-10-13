# Employee Attendance and Analytics API

Python 3.11+, FastAPI, and MongoDB 6.0+ (tested locally against MongoDB 7).

```sh
python -m venv .venv
pip install -r requirements.txt
```

Activate the environment before installing. Set `MONGO_URI` and `MONGO_DB` in
your shell, then run `uvicorn app.main:app --port 8000`. An optional local `.env`
is loaded without overriding shell variables. Indexes are created at startup;
application startup never seeds or deletes data.

For tests, install `requirements-dev.txt` and run `python -m pytest`.

Implementation progress: validation, UTC/IST calculations, serialization,
startup indexes, and readiness are implemented. CRUD and analytics are next.
