from datetime import datetime, timedelta, timezone
import time

import pytest

pytestmark = pytest.mark.integration


def walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def test_explain_required_parameters_and_bounds(api, database):
    for endpoint in ("employee_monthly", "department_summary", "late_leaderboard", "department_trend", "unknown"):
        assert api.get("/admin/explain/" + endpoint).status_code == 422
    assert api.get("/admin/explain/attendance_list", params={"page": 0}).status_code == 422
    assert api.get("/admin/explain/department_trend", params={"department": "Eng", "from": "2026-07-03", "to": "2026-07-01"}).status_code == 422


@pytest.mark.parametrize("filters", [{}, {"status": "WFH"}, {"emp_code": "EMP0001"}, {"date_from": "2026-07-01", "date_to": "2026-07-31"}])
def test_explain_list_uses_exact_filter_and_pagination(api, database, filters):
    response = api.get("/admin/explain/attendance_list", params=filters | {"page": 2, "page_size": 7})
    assert response.status_code == 200, response.text
    payload = response.json()
    command = payload["explain"]["command"]
    assert command["skip"] == 7 and command["limit"] == 7
    assert command["sort"] == {"date": -1, "emp_code": 1}
    assert payload["collection"] == "attendance_logs"
    assert "executionStats" in payload["explain"]


def test_scale_100k_indexes_and_all_main_queries(api, database):
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    departments = ("Eng", "Sales", "Support")
    employees = [{"emp_code": f"EMP{i:04d}", "name": f"Employee {i}", "email": f"e{i}@example.com",
                  "department": departments[i % 3], "shift_start": "09:30", "shift_end": "18:30",
                  "joined_on": "2026-01-01", "created_at": created} for i in range(1, 1001)]
    database.employees.insert_many(employees)
    first = datetime(2026, 6, 1, tzinfo=timezone.utc)
    logs = []
    for offset in range(100):
        day = first + timedelta(days=offset)
        for i in range(1, 1001):
            logs.append({"emp_code": f"EMP{i:04d}", "date": day.date().isoformat(), "status": "WFH" if i % 5 == 0 else "PRESENT",
                         "punch_in": day + timedelta(hours=4), "punch_out": day + timedelta(hours=13), "work_hours": 9.0,
                         "late_minutes": i % 4 * 10, "overtime_minutes": 0, "half_day": False})
        if len(logs) >= 10000:
            database.attendance_logs.insert_many(logs)
            logs.clear()
    assert database.attendance_logs.count_documents({}) == 100000
    database.employees.drop_indexes()
    database.attendance_logs.drop_indexes()
    from app.main import create_indexes
    started = time.monotonic()
    create_indexes()
    cold_indexes = time.monotonic() - started
    assert cold_indexes < 20
    print("cold index creation seconds", round(cold_indexes, 3))
    operations = [
        ("attendance_list", {}), ("attendance_list", {"status": "WFH"}),
        ("attendance_list", {"emp_code": "EMP0001", "status": "PRESENT", "page": 2}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("department_summary", {"month": "2026-07", "department": "Eng"}),
        ("late_leaderboard", {"month": "2026-07", "limit": 2}),
        ("late_leaderboard", {"month": "2026-07", "limit": 2, "department": "Eng"}),
        ("department_trend", {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"}),
    ]
    for endpoint, params in operations:
        started = time.monotonic()
        response = api.get("/admin/explain/" + endpoint, params=params)
        assert response.status_code == 200, response.text
        explanation = response.json()["explain"]
        nodes = list(walk(explanation))
        assert any(node.get("stage") == "IXSCAN" for node in nodes), endpoint
        assert not any(node.get("stage") == "COLLSCAN" for node in nodes), endpoint
        assert not any(node.get("collectionScans", 0) > 0 for node in nodes), endpoint
        assert any("executionStats" in node or "executionTimeMillisEstimate" in node for node in nodes)
        print(endpoint, params, "seconds", round(time.monotonic() - started, 3))
    page = api.get("/attendance", params={"page_size": 100}).json()
    assert page["total"] == 100000 and len(page["items"]) == 100
