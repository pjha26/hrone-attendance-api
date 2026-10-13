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


def employee_or_404(emp_code):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "Employee not found")
    return employee


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    if date_from is not None and date_to is not None and date_from > date_to:
        invalid("date_from must not be after date_to", "query")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    hint = "attendance_employee_date" if emp_code is not None else (
        "attendance_status_date" if status is not None else "attendance_date_employee"
    )
    return query, hint


def snapshot_filter(doc):
    # Compare both computed inputs and history length: stale edits cannot erase newer work.
    expected = material_state(doc)
    conditions = [{"$eq": [{"$ifNull": ["$" + key, MATERIAL_DEFAULTS[key]]}, {"$literal": value}]}
                  for key, value in expected.items()]
    conditions.append({"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(doc.get("history", []))]})
    return {"emp_code": doc["emp_code"], "date": doc["date"], "$expr": {"$and": conditions}}


def validate_punch_pair(state):
    if state["punch_out"] is not None:
        seconds = (state["punch_out"] - state["punch_in"]).total_seconds()
        if seconds <= 0 or seconds > 86400:
            invalid("punch_out must be after punch_in and within 24 hours")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return employee_output(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    hint = "employee_code" if department is None else "employee_department_code"
    total = db.employees.count_documents(query, hint=hint)
    docs = db.employees.find(query, {"_id": 0}).hint(hint).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_output(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = employee_or_404(body.emp_code)
    punch = instant(body.punched_at)
    day = attendance_date(punch, employee)
    state = {"status": body.status, "punch_in": punch, "punch_out": None}
    doc = {"emp_code": body.emp_code, "date": day, **state, **derived_fields(state, employee, day), "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date") from None
    return attendance_output(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None, date_from: DateText | None = None, date_to: DateText | None = None,
    status: Status | None = None, page: Page = 1, page_size: PageSize = 20,
):
    query, hint = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query, hint=hint)
    docs = db.attendance_logs.find(query, {"_id": 0}).hint(hint).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_output(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = employee_or_404(body.emp_code)
    punch = instant(body.punched_at)
    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$type": "date", "$lte": punch}},
        sort=[("punch_in", -1)], hint="attendance_employee_punch",
    )
    if doc is None:
        raise HTTPException(404, "No punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    state = material_state(doc)
    state["punch_out"] = punch
    validate_punch_pair(state)
    state.update(derived_fields(state, employee, doc["date"]))
    updated = db.attendance_logs.find_one_and_update(snapshot_filter(doc), {"$set": state}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "Attendance changed concurrently; retry")
    return attendance_output(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateText, body: RegularizeIn):
    employee = employee_or_404(emp_code)
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if doc is None:
        raise HTTPException(404, "Attendance record not found")
    before = material_state(doc)
    after = before.copy()
    supplied = body.model_fields_set
    if "status" in supplied:
        after["status"] = body.status
    for key in ("punch_in", "punch_out"):
        if key in supplied:
            after[key] = instant(getattr(body, key))
    if after["status"] not in PRESENCE:
        if supplied & {"punch_in", "punch_out"}:
            invalid("ABSENT or LEAVE cannot be supplied with punch times")
    else:
        if after["punch_in"] is None:
            invalid("Presence status requires punch_in")
        if attendance_date(after["punch_in"], employee) != date:
            invalid("punch_in must remain on the attendance date")
        validate_punch_pair(after)
    after.update(derived_fields(after, employee, date))
    changes = {key: {"from": before[key], "to": value} for key, value in after.items() if before[key] != value}
    if not changes:
        invalid("Correction changes nothing")
    history = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        snapshot_filter(doc), {"$set": after, "$push": {"history": history}}, return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Attendance changed concurrently; retry")
    return attendance_output(updated)
