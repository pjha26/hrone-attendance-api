from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import pytest

from app.main import IST, epoch_millis

pytestmark = pytest.mark.integration


def ms(text):
    return epoch_millis(datetime.fromisoformat(text).replace(tzinfo=IST))


def employee(api, code="EMP0001", **changes):
    body = dict(emp_code=code, name=code, email=code + "@example.com", department="Eng", joined_on="2026-07-01")
    response = api.post("/employees", json=body | changes)
    assert response.status_code == 201, response.text
    return response.json()


def start(api, code="EMP0001", when="2026-07-06T09:40:01", **changes):
    response = api.post("/attendance/punch-in", json=dict(emp_code=code, punched_at=ms(when)) | changes)
    assert response.status_code == 201, response.text
    return response.json()


def test_employee_pagination_filters_and_duplicate(api, database):
    for code in ("EMP0003", "EMP0001", "EMP0002"):
        result = employee(api, code, department="Eng" if code != "EMP0002" else "Sales")
        assert type(result["created_at"]) is int and "_id" not in result
    page = api.get("/employees", params={"department": "Eng", "page_size": 1}).json()
    assert page["total"] == 2 and page["items"][0]["emp_code"] == "EMP0001"
    assert api.get("/employees", params={"department": "Eng", "page_size": 1, "page": 2}).json()["items"][0]["emp_code"] == "EMP0003"
    assert api.post("/employees", json={"emp_code": "EMP0001", "name": "A", "email": "a@example.com", "department": "Eng", "joined_on": "2026-07-01"}).status_code == 409


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 101}, {"date_from": "2026-02-30"}, {"date_from": "2026-07-07", "date_to": "2026-07-06"}, {"status": "BAD"}])
def test_invalid_list_inputs(api, database, params):
    assert api.get("/attendance", params=params).status_code == 422


def test_punch_pair_and_audited_correction(api, database):
    employee(api)
    result = start(api, work_hours=999, half_day=True)
    assert result["late_minutes"] == 10 and result["work_hours"] is None and result["history"] == []
    result = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T19:00:00")}).json()
    assert result["overtime_minutes"] == 30 and result["history"] == []
    patch = {"punch_in": ms("2026-07-06T09:28:00"), "regularized_by": "hr", "reason": "clock correction"}
    response = api.patch("/attendance/EMP0001/2026-07-06", json=patch)
    assert response.status_code == 200, response.text
    result = response.json()
    changes = result["history"][0]["changes"]
    assert changes["late_minutes"] == {"from": 10, "to": 0}
    assert type(changes["punch_in"]["to"]) is int
    assert "status" not in changes
    assert result["history"][0]["by"] == "hr"
    assert api.patch("/attendance/EMP0001/2026-07-06", json=patch).status_code == 422
    leave = {"status": "LEAVE", "regularized_by": "hr", "reason": "leave approved"}
    result = api.patch("/attendance/EMP0001/2026-07-06", json=leave).json()
    assert result["punch_in"] is None and result["punch_out"] is None and result["work_hours"] is None
    assert result["late_minutes"] == 0 and len(result["history"]) == 2


def test_overnight_and_latest_closed_record(api, database):
    employee(api, shift_start="22:00", shift_end="06:00")
    start(api, when="2026-07-06T21:55:00")
    result = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07T06:40:00")})
    assert result.status_code == 200 and result.json()["date"] == "2026-07-06"
    assert result.json()["work_hours"] == 8.75 and result.json()["overtime_minutes"] == 40
    start(api, when="2026-07-05T22:00:00")
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07T07:00:00")}).status_code == 409


def test_error_codes_and_patch_constraints(api, database):
    assert api.post("/attendance/punch-in", json={"emp_code": "unknown"}).status_code == 404
    employee(api)
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    start(api, when="2026-07-06T09:30:00")
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T09:30:00")}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T09:29:59")}).status_code == 404
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07T09:30:01")}).status_code == 422
    base = {"regularized_by": "HR", "reason": "correct clock"}
    for changes in ({"punch_in": ms("2026-07-07T09:30:00")}, {"status": "ABSENT", "punch_out": ms("2026-07-06T18:00:00")}, {"punch_out": None}, {}):
        assert api.patch("/attendance/EMP0001/2026-07-06", json=base | changes).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-07", json=base).status_code == 404
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07T09:30:00")}).status_code == 200


def test_legacy_list_defaults_and_sort(api, database):
    for code in ("EMP0002", "EMP0001"):
        employee(api, code)
        database.attendance_logs.insert_one({"emp_code": code, "date": "2026-07-06", "status": "ABSENT"})
    result = api.get("/attendance", params={"status": "ABSENT", "page_size": 1}).json()
    assert result["total"] == 2 and result["items"][0]["emp_code"] == "EMP0001"
    assert result["items"][0]["history"] == [] and result["items"][0]["half_day"] is False


def parallel(action, count=12):
    barrier = Barrier(count)
    def run(_):
        barrier.wait(timeout=10)
        return action()
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(run, range(count)))


def test_duplicate_create_and_punch_races(api, database):
    body = {"emp_code": "EMP0001", "name": "A", "email": "a@example.com", "department": "Eng", "joined_on": "2026-07-01"}
    codes = parallel(lambda: api.post("/employees", json=body).status_code)
    assert codes.count(201) == 1 and codes.count(409) == 11
    codes = parallel(lambda: api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T09:30:00")}).status_code)
    assert codes.count(201) == 1 and codes.count(409) == 11
    codes = parallel(lambda: api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T18:30:00")}).status_code)
    assert codes.count(200) == 1 and codes.count(409) == 11
    assert database.attendance_logs.count_documents({}) == 1


def test_concurrent_regularizations_preserve_audit(api, database):
    employee(api)
    start(api)
    def correct():
        return api.patch("/attendance/EMP0001/2026-07-06", json={"status": "WFH", "regularized_by": "HR", "reason": "remote attendance"}).status_code
    codes = parallel(correct)
    assert codes.count(200) == 1
    assert set(codes) <= {200, 409, 422}
    assert len(database.attendance_logs.find_one({})["history"]) == 1
