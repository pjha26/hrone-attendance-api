# Employee Attendance and Analytics API

FastAPI and MongoDB API for employee creation, attendance punches, audited
corrections, monthly summaries, late rankings, and daily department trends.
All application code lives in `app/main.py`. Requires Python 3.11+ and
MongoDB 6.0+; integration tests use MongoDB 7.

## Run

```sh
python -m venv .venv
# Activate: source .venv/bin/activate (Linux/macOS)
# Activate: .\.venv\Scripts\Activate.ps1 (PowerShell)
pip install -r requirements.txt
```

Set configuration in the shell. PowerShell example:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

Linux/macOS: `export MONGO_URI=mongodb://localhost:27017` and
`export MONGO_DB=attendance_db`, then use the same Uvicorn command.
An optional local `.env` is loaded without overriding real environment
variables. Indexes are created automatically. Startup does not seed, delete,
or migrate existing data. `GET /health` checks database connectivity.

The collections are `employees` and `attendance_logs`. Employee codes are
business identifiers. Instants are integer epoch milliseconds in the API
and UTC BSON datetimes in storage; calendar dates and shift times use IST.

## Tests

```sh
pip install -r requirements-dev.txt
python -m pytest -q
```

Set `TEST_MONGO_URI` to a MongoDB test server to include integration tests:

```powershell
$env:TEST_MONGO_URI = "mongodb://localhost:27017"
python -m pytest -q
```

Tests create and drop a uniquely named `hrone_test_<uuid>` database, separate
from `MONGO_DB`. Without `TEST_MONGO_URI`, integration tests are skipped.
The suite covers time/rounding boundaries, validation, pagination, races,
audit history, zero-log employees, joiners, weekend records, ranking ties,
empty trend ranges, and real execution plans with 100,000 attendance records.

## Endpoints

| Method | Path |
|---|---|
| GET | `/health` |
| POST / GET | `/employees` |
| POST | `/attendance/punch-in` |
| POST | `/attendance/punch-out` |
| GET | `/attendance` |
| PATCH | `/attendance/{emp_code}/{date}` |
| GET | `/analytics/employees/{emp_code}/monthly?month=YYYY-MM` |
| GET | `/analytics/departments/summary?month=YYYY-MM` |
| GET | `/analytics/leaderboard/late?month=YYYY-MM&limit=10` |
| GET | `/analytics/departments/{department}/trend?from=YYYY-MM-DD&to=YYYY-MM-DD` |
| GET | `/admin/explain/{endpoint}` |

Interactive API documentation is available at `/docs`. Explain accepts
`attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`,
or `department_trend` with that endpoint's parameters. It executes the same
query/pipeline with `executionStats`; raw output is in the `explain` field.

No required endpoints are omitted. There is no authentication or holiday
calendar in this assignment. REVIEW.md records the starter fixes;
DECISIONS.md explains the database choices.
