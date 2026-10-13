"""Attendance API. Calendar values use IST; stored instants use UTC."""

import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import Decimal128, ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMillis = Annotated[StrictInt, Field(ge=100000000000, le=4102444800000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
    connectTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def create_indexes():
    db.employees.create_index("emp_code", unique=True, name="employee_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="employee_department_code")
    db.employees.create_index([("joined_on", 1), ("department", 1)], name="employee_join_department")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="employee_department_join")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="attendance_employee_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="attendance_employee_punch")
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="attendance_status_date")


@asynccontextmanager
async def lifespan(app):
    create_indexes()
    try:
        yield
    finally:
        client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_unavailable(request, exc):
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


def valid_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Use YYYY-MM-DD")
    date.fromisoformat(value)
    return value


def valid_month(value: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("Use YYYY-MM")
    date.fromisoformat(value + "-01")
    return value


DateText = Annotated[str, AfterValidator(valid_date)]
MonthText = Annotated[str, AfterValidator(valid_month)]
ShiftTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class RequestBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null(cls, data):
        if isinstance(data, dict):
            for key in ("punched_at", "punch_in", "punch_out", "status"):
                if key in cls.model_fields and key in data and data[key] is None:
                    raise ValueError(f"{key} may be omitted but cannot be null")
        return data


class EmployeeIn(RequestBody):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: DateText

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchOutIn(RequestBody):
    emp_code: str
    punched_at: EpochMillis | None = None


class PunchInIn(PunchOutIn):
    status: PresenceStatus = "PRESENT"


class RegularizeIn(RequestBody):
    status: Status | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def invalid(message, location="body"):
    raise HTTPException(422, detail=[{"type": "value_error", "loc": [location], "msg": message}])


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def instant(milliseconds):
    return now_utc() if milliseconds is None else EPOCH + timedelta(seconds=milliseconds // 1000)


def epoch_millis(value):
    # Naive BSON dates are UTC, never the host's local timezone.
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    delta = value - EPOCH
    return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000


def json_value(value):
    if isinstance(value, datetime):
        return epoch_millis(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at")
MATERIAL_DEFAULTS = {
    "status": None, "punch_in": None, "punch_out": None, "work_hours": None,
    "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
}


def employee_output(doc):
    return json_value({key: doc[key] for key in EMPLOYEE_FIELDS})


def material_state(doc):
    return {key: doc.get(key, default) for key, default in MATERIAL_DEFAULTS.items()}


def attendance_output(doc):
    return json_value({"emp_code": doc["emp_code"], "date": doc["date"], **material_state(doc), "history": doc.get("history", [])})


def attendance_date(punch, employee):
    local = punch.astimezone(IST)
    result = local.date()
    if employee["shift_end"] <= employee["shift_start"] and local.time() < time.fromisoformat(employee["shift_end"]):
        result -= timedelta(days=1)
    return result.isoformat()


def shift_bounds(day, employee):
    day = date.fromisoformat(day)
    start = datetime.combine(day, time.fromisoformat(employee["shift_start"]), IST)
    end = datetime.combine(day, time.fromisoformat(employee["shift_end"]), IST)
    if employee["shift_end"] <= employee["shift_start"]:
        end += timedelta(days=1)
    return start, end


def derived_fields(state, employee, day):
    if state["status"] not in PRESENCE:
        return {"punch_in": None, "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    start, end = shift_bounds(day, employee)
    elapsed = int((state["punch_in"] - start).total_seconds())
    result = {"late_minutes": elapsed // 60 if elapsed > 600 else 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if state["punch_out"] is not None:
        seconds = int((state["punch_out"] - state["punch_in"]).total_seconds())
        hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        overtime = max(0, int((state["punch_out"] - end).total_seconds()) // 60)
        result.update(work_hours=float(hours), overtime_minutes=overtime if overtime >= 30 else 0, half_day=hours < Decimal("4.50"))
    return result


@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "Database unavailable") from None
    return {"status": "ok"}
