from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.integration


def emp(database, code, department="Eng", joined="2026-07-01"):
    database.employees.insert_one({"emp_code": code, "name": code, "email": code + "@example.com", "department": department,
                                   "joined_on": joined, "shift_start": "09:30", "shift_end": "18:30", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})


def log(database, code, day, status="PRESENT", **fields):
    database.attendance_logs.insert_one({"emp_code": code, "date": day, "status": status, "punch_in": None, "punch_out": None,
                                        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, **fields})


def get(api, path, **params):
    response = api.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_monthly_weekends_joiners_and_stored_values(api, database):
    emp(database, "EMP0001", joined="2026-07-13")
    log(database, "EMP0001", "2026-07-13", work_hours=8.0, late_minutes=10)
    log(database, "EMP0001", "2026-07-14", "WFH", half_day=True, work_hours=3.0)
    log(database, "EMP0001", "2026-07-18", "ON_DUTY", work_hours=6.0, late_minutes=30, overtime_minutes=50)
    log(database, "EMP0001", "2026-07-19", "LEAVE")
    result = get(api, "/analytics/employees/EMP0001/monthly", month="2026-07")
    assert result == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 15, "present_days": 1.5,
                      "leave_days": 1, "late_count": 2, "total_late_minutes": 40, "total_overtime_minutes": 50, "attendance_pct": 10.0}
    emp(database, "EMP0002")
    result = get(api, "/analytics/employees/EMP0002/monthly", month="2026-07")
    assert result["working_days"] == 23 and result["attendance_pct"] == 0
    emp(database, "EMP0003", joined="2026-08-01")
    result = get(api, "/analytics/employees/EMP0003/monthly", month="2026-07")
    assert result["working_days"] == 0 and result["attendance_pct"] is None
    assert api.get("/analytics/employees/unknown/monthly", params={"month": "2026-07"}).status_code == 404


def test_department_headcount_and_record_weighted_average(api, database):
    for code in ("EMP0001", "EMP0002", "EMP0003"):
        emp(database, code)
    emp(database, "EMP0004", joined="2026-08-01")
    emp(database, "EMP0005", "Sales")
    log(database, "EMP0001", "2026-07-06", work_hours=8)
    log(database, "EMP0001", "2026-07-07", "WFH", work_hours=8)
    log(database, "EMP0002", "2026-07-08", "ON_DUTY", work_hours=4, half_day=True, late_minutes=15)
    log(database, "EMP0002", "2026-07-11", "LEAVE")
    log(database, "EMP0004", "2026-07-12", work_hours=20)
    result = get(api, "/analytics/departments/summary", month="2026-07")["items"]
    assert result[0] == {"department": "Eng", "headcount": 3, "present_days": 2.5, "avg_work_hours": 6.67,
                         "late_count": 1, "total_late_minutes": 15, "leave_count": 1, "on_duty_count": 1}
    assert result[1]["department"] == "Sales" and result[1]["avg_work_hours"] is None and result[1]["headcount"] == 1
    assert len(get(api, "/analytics/departments/summary", month="2026-07", department="Sales")["items"]) == 1


def test_competition_ranking_ties_and_department_filter(api, database):
    for code, minutes in (("EMP0004", 20), ("EMP0003", 40), ("EMP0002", 40), ("EMP0001", 60)):
        emp(database, code, "Sales" if code == "EMP0001" else "Eng")
        log(database, code, "2026-07-06", late_minutes=minutes)
    log(database, "orphan", "2026-07-06", late_minutes=1000)
    for limit in (2, 3):
        rows = get(api, "/analytics/leaderboard/late", month="2026-07", limit=limit)["items"]
        assert [row["rank"] for row in rows] == [1, 2, 2]
        assert [row["emp_code"] for row in rows] == ["EMP0001", "EMP0002", "EMP0003"]
    rows = get(api, "/analytics/leaderboard/late", month="2026-07", limit=1, department="Eng")["items"]
    assert [row["rank"] for row in rows] == [1, 1]
    rows = get(api, "/analytics/leaderboard/late", month="2026-07", limit=4)["items"]
    assert [row["rank"] for row in rows] == [1, 2, 2, 4]


def test_trend_calendar_headcount_weekends_and_window(api, database):
    emp(database, "EMP0001", joined="2026-07-01")
    emp(database, "EMP0002", joined="2026-07-08")
    log(database, "EMP0001", "2026-07-06")
    log(database, "EMP0001", "2026-07-08", half_day=True, late_minutes=20)
    log(database, "EMP0001", "2026-07-11", late_minutes=30)
    rows = get(api, "/analytics/departments/Eng/trend", **{"from": "2026-07-06", "to": "2026-07-13"})["items"]
    assert len(rows) == 8
    assert [row["headcount"] for row in rows] == [1, 1, 2, 2, 2, 2, 2, 2]
    assert [row["attendance_rate"] for row in rows] == [1, 0, 0.25, 0, 0, None, None, 0]
    assert [row["moving_avg_7d"] for row in rows] == [1, 0.5, 0.4167, 0.3125, 0.25, 0.25, 0.25, 0.05]
    assert rows[5]["present_count"] == 1 and rows[5]["late_count"] == 1 and rows[5]["is_working_day"] is False


def test_empty_trend_future_joiners_and_range_validation(api, database):
    emp(database, "EMP0001", joined="2026-08-01")
    rows = get(api, "/analytics/departments/Eng/trend", **{"from": "2026-07-01", "to": "2026-07-03"})["items"]
    assert len(rows) == 3 and all(row["headcount"] == 0 and row["attendance_rate"] is None and row["moving_avg_7d"] is None for row in rows)
    rows = get(api, "/analytics/departments/Eng/trend", **{"from": "2026-07-01", "to": "2026-09-30"})["items"]
    assert len(rows) == 92
    assert rows[-1]["attendance_rate"] == 0
    for first, last in (("2026-07-01", "2026-10-01"), ("2026-07-03", "2026-07-01")):
        assert api.get("/analytics/departments/Eng/trend", params={"from": first, "to": last}).status_code == 422
    assert api.get("/analytics/departments/unknown/trend", params={"from": "2026-07-01", "to": "2026-07-01"}).status_code == 404
    assert api.get("/analytics/departments/summary", params={"month": "2026-13"}).status_code == 422
