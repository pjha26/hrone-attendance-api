from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.main import (
    EmployeeIn, IST, PunchInIn, RegularizeIn, attendance_date, attendance_output,
    derived_fields, epoch_millis, instant, valid_date, valid_month,
)

DAY_SHIFT = {"shift_start": "09:30", "shift_end": "18:30"}


def local(value):
    return datetime.fromisoformat(value).replace(tzinfo=IST)


@pytest.mark.parametrize("value", [1783312500, 1783312500000.0, "1783312500000", True, None, 4102444800001])
def test_strict_supplied_timestamps(value):
    with pytest.raises(ValidationError):
        PunchInIn(emp_code="EMP0001", punched_at=value)


@pytest.mark.parametrize("time_text,expected", [("09:40:00.900", 0), ("09:40:01", 10), ("10:15:59", 45)])
def test_grace_is_checked_in_seconds(time_text, expected):
    punch = instant(epoch_millis(local("2026-07-06T" + time_text)))
    assert punch.microsecond == 0
    state = {"status": "PRESENT", "punch_in": punch, "punch_out": None}
    assert derived_fields(state, DAY_SHIFT, "2026-07-06")["late_minutes"] == expected


@pytest.mark.parametrize("seconds,hours,half_day", [(16146, 4.49, True), (16182, 4.50, False), (16200, 4.50, False)])
def test_decimal_half_up_and_rounded_half_day(seconds, hours, half_day):
    from datetime import timedelta
    punch = local("2026-07-06T09:30:00")
    result = derived_fields({"status": "WFH", "punch_in": punch, "punch_out": punch + timedelta(seconds=seconds)}, DAY_SHIFT, "2026-07-06")
    assert result["work_hours"] == hours
    assert result["half_day"] is half_day


@pytest.mark.parametrize("end,expected", [("18:59:59", 0), ("19:00:00", 30)])
def test_overtime_minimum(end, expected):
    state = {"status": "ON_DUTY", "punch_in": local("2026-07-06T09:30:00"), "punch_out": local("2026-07-06T" + end)}
    assert derived_fields(state, DAY_SHIFT, "2026-07-06")["overtime_minutes"] == expected


def test_overnight_date_and_shift_end():
    employee = {"shift_start": "22:00", "shift_end": "06:00"}
    assert attendance_date(local("2026-07-07T05:59:59"), employee) == "2026-07-06"
    assert attendance_date(local("2026-07-07T06:00:00"), employee) == "2026-07-07"
    state = {"status": "PRESENT", "punch_in": local("2026-07-06T21:55:00"), "punch_out": local("2026-07-07T06:40:00")}
    assert derived_fields(state, employee, "2026-07-06")["overtime_minutes"] == 40


def test_serialization_nested_dates_and_legacy_defaults():
    dt = datetime(2026, 7, 6, tzinfo=timezone.utc)
    naive = dt.replace(tzinfo=None)
    assert epoch_millis(dt) == epoch_millis(naive)
    doc = {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "punch_in": dt,
           "history": [{"at": dt, "changes": {"punch_in": {"from": None, "to": dt}}}]}
    result = attendance_output(doc)
    assert result["half_day"] is False
    assert result["history"][0]["changes"]["punch_in"]["to"] == epoch_millis(dt)
    assert "id" not in result and "_id" not in result


def test_contract_validation_and_ignored_derived_fields():
    employee = dict(emp_code="EMP0001", name="A", email="a@example.com", department="Eng", joined_on="2026-07-01")
    assert EmployeeIn(**employee).shift_start == "09:30"
    for changes in ({"joined_on": "2026-02-30"}, {"shift_end": "09:30"}, {"emp_code": "EMP1"}, {"email": "bad"}):
        with pytest.raises(ValidationError):
            EmployeeIn(**(employee | changes))
    body = PunchInIn(emp_code="EMP0001", late_minutes=999)
    assert "late_minutes" not in body.model_dump()
    with pytest.raises(ValidationError):
        RegularizeIn(reason="correction", regularized_by="HR", punch_out=None)
    with pytest.raises(ValueError):
        valid_date("2026-7-01")
    with pytest.raises(ValueError):
        valid_month("0000-07")
