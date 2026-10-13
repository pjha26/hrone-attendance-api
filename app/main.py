"""Attendance API. Calendar values use IST; stored instants use UTC."""

import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, model_validator
from pydantic.json_schema import SkipJsonSchema
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
    try:
        create_indexes()
        yield
    finally:
        client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan, docs_url=None,
    description="Manage attendance, correct records with an audit trail, and explore MongoDB analytics. "
                "Instants use integer epoch milliseconds; dates and shift times use IST. "
                "Start by creating an employee, then record a punch-in and punch-out.",
    openapi_tags=[
        {"name": "system", "description": "Check database readiness."},
        {"name": "employees", "description": "Create employees and browse filtered pages."},
        {"name": "attendance", "description": "Record punches, browse attendance, and make audited corrections."},
        {"name": "analytics", "description": "Monthly summaries, late rankings, and daily department trends."},
        {"name": "admin", "description": "Inspect the execution plans used by list and analytics queries."},
    ],
)


@app.get("/docs", include_in_schema=False)
def swagger_docs(request: Request):
    root_path = request.scope.get("root_path", "").rstrip("/")
    html = get_swagger_ui_html(
        openapi_url=root_path + app.openapi_url, title=app.title + " - Swagger UI",
        swagger_ui_parameters=app.swagger_ui_parameters,
    )
    style = """<style>
    body { margin: 0; background: #fff; }
    .swagger-ui { font-family: "Segoe UI", system-ui, sans-serif; color: #243246; line-height: 1.5; }
    .swagger-ui * { font-family: inherit; }
    .swagger-ui code, .swagger-ui pre, .swagger-ui textarea, .swagger-ui .opblock-summary-path {
        font-family: Consolas, "Liberation Mono", monospace;
    }
    .swagger-ui .wrapper { max-width: 1160px; padding: 0 24px; }
    .swagger-ui .info { margin: 40px 0 28px; }
    .swagger-ui .info .title { font-size: clamp(24px, 2.5vw, 32px); font-weight: 600; letter-spacing: -.5px; line-height: 1.3; }
    .swagger-ui .info .title small { font-size: 12px; vertical-align: middle; }
    .swagger-ui .info .description { max-width: 80ch; }
    .swagger-ui .info p { font-size: 14px; line-height: 1.7; }
    .swagger-ui .opblock-tag { font-size: 20px; font-weight: 600; padding: 14px 10px; margin: 22px 0 12px; }
    .swagger-ui .opblock-tag small { font-size: 14px; font-weight: 400; color: #526176; }
    .swagger-ui .opblock { border-radius: 8px; box-shadow: none; margin-bottom: 12px; }
    .swagger-ui .opblock .opblock-summary { padding: 8px 10px; }
    .swagger-ui .opblock .opblock-summary-method { font-size: 12px; min-width: 72px; }
    .swagger-ui .opblock .opblock-summary-path { font-size: 14px; }
    .swagger-ui .opblock .opblock-summary-description { font-size: 14px; }
    .swagger-ui .btn, .swagger-ui input, .swagger-ui select { border-radius: 6px; }
    .swagger-ui textarea { line-height: 1.6; }
    @media (max-width: 640px) {
        .swagger-ui .wrapper { padding: 0 14px; }
        .swagger-ui .opblock-tag { flex-wrap: wrap; gap: 6px; }
        .swagger-ui .opblock-tag small { padding-left: 0; }
    }
    </style>"""
    return HTMLResponse(html.body.decode("utf-8").replace("</head>", style + "</head>"))


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
ShiftTime = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]


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
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "emp_code": "EMP0001", "name": "Test Employee", "email": "test@example.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-01",
    }]})
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
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
    model_config = ConfigDict(json_schema_extra={"examples": [{"emp_code": "EMP0001", "punched_at": 1783345200000}]})
    emp_code: str
    punched_at: EpochMillis | SkipJsonSchema[None] = None


class PunchInIn(PunchOutIn):
    model_config = ConfigDict(json_schema_extra={"examples": [{"emp_code": "EMP0001", "punched_at": 1783312500000, "status": "PRESENT"}]})
    status: PresenceStatus = "PRESENT"


class RegularizeIn(RequestBody):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "punch_in": 1783310280000, "reason": "Corrected biometric clock entry", "regularized_by": "hr.admin",
    }]})
    status: Status | SkipJsonSchema[None] = None
    punch_in: EpochMillis | SkipJsonSchema[None] = None
    punch_out: EpochMillis | SkipJsonSchema[None] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# These models describe responses in OpenAPI; serializers still control runtime output.
class HealthOut(BaseModel):
    status: Literal["ok"]


class ErrorOut(BaseModel):
    detail: str


NOT_FOUND = {"model": ErrorOut, "description": "Employee, attendance record, or department not found."}
CONFLICT = {"model": ErrorOut, "description": "Duplicate record, already closed attendance, or a concurrent update."}
UNAVAILABLE = {"model": ErrorOut, "description": "MongoDB is unavailable."}


class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: EpochMillis


class EmployeePageOut(BaseModel):
    items: list[EmployeeOut]
    total: int
    page: int
    page_size: int


class FieldChangeOut(BaseModel):
    previous: str | int | float | bool | None = Field(alias="from")
    to: str | int | float | bool | None


class HistoryOut(BaseModel):
    at: EpochMillis
    by: str
    reason: str
    changes: dict[str, FieldChangeOut]


class AttendanceOut(BaseModel):
    emp_code: str
    date: str
    status: Status
    punch_in: EpochMillis | None
    punch_out: EpochMillis | None
    work_hours: float | None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryOut]


class AttendancePageOut(BaseModel):
    items: list[AttendanceOut]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyOut(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: float | None


class DepartmentItemOut(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: float | None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryOut(BaseModel):
    month: str
    items: list[DepartmentItemOut]


class LeaderboardItemOut(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardOut(BaseModel):
    month: str
    items: list[LeaderboardItemOut]


class TrendItemOut(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: float | None
    moving_avg_7d: float | None


class TrendOut(BaseModel):
    department: str
    items: list[TrendItemOut]


class ExplainOut(BaseModel):
    endpoint: str
    collection: str
    explain: dict


def invalid(message, location="body"):
    raise HTTPException(422, detail=[{"type": "value_error", "loc": [location], "msg": message}])


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def instant(milliseconds):
    return now_utc() if milliseconds is None else EPOCH + timedelta(seconds=milliseconds // 1000)


def epoch_millis(value):
    # Naive BSON dates are UTC, never the host's local timezone.
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    delta = value.replace(microsecond=0) - EPOCH
    return delta.days * 86400000 + delta.seconds * 1000


def json_value(value):
    if isinstance(value, datetime):
        return epoch_millis(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
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


@app.get("/health", tags=["system"], operation_id="health", summary="Check database readiness",
         responses={200: {"model": HealthOut}, 503: UNAVAILABLE})
def health():
    """Return ready only when MongoDB responds to a ping."""
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


def attendance_find_command(emp_code=None, date_from=None, date_to=None, status=None, page=1, page_size=20):
    query, hint = attendance_query(emp_code, date_from, date_to, status)
    return {"find": "attendance_logs", "filter": query, "projection": {"_id": 0},
            "sort": {"date": -1, "emp_code": 1}, "skip": page_offset(page, page_size), "limit": page_size, "hint": hint}


def page_offset(page, page_size):
    # MongoDB encodes skip as int64; larger offsets still mean an empty page.
    return min((page - 1) * page_size, 2**63 - 1)


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


def truncate_punches(state):
    for key in ("punch_in", "punch_out"):
        if state[key] is not None:
            state[key] = state[key].replace(microsecond=0)


@app.post("/employees", status_code=201, tags=["employees"], operation_id="createEmployee", summary="Create an employee",
          responses={201: {"model": EmployeeOut}, 409: CONFLICT, 503: UNAVAILABLE})
def create_employee(body: EmployeeIn):
    """Supply the customer's unique employee code. The server sets created_at; duplicate codes return 409."""
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return employee_output(doc)


@app.get("/employees", tags=["employees"], operation_id="listEmployees", summary="List employees",
         responses={200: {"model": EmployeePageOut}, 503: UNAVAILABLE})
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    """Filter by an exact department name. Pages are ordered by employee code; total counts the filtered set."""
    query = {} if department is None else {"department": department}
    hint = "employee_code" if department is None else "employee_department_code"
    total = db.employees.count_documents(query, hint=hint)
    docs = db.employees.find(query, {"_id": 0}).hint(hint).sort("emp_code", 1).skip(page_offset(page, page_size)).limit(page_size)
    return {"items": [employee_output(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"], operation_id="punchIn", summary="Record a punch-in",
          responses={201: {"model": AttendanceOut}, 404: NOT_FOUND, 409: CONFLICT, 503: UNAVAILABLE})
def punch_in(body: PunchInIn):
    """Omit punched_at to use now. The IST shift determines the date; only one record per employee/day is allowed."""
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


@app.get("/attendance", tags=["attendance"], operation_id="listAttendance", summary="List attendance records",
         responses={200: {"model": AttendancePageOut}, 503: UNAVAILABLE})
def list_attendance(
    emp_code: str | None = None, date_from: DateText | None = None, date_to: DateText | None = None,
    status: Status | None = None, page: Page = 1, page_size: PageSize = 20,
):
    """Dates are inclusive. Results are sorted by date descending, then employee code ascending."""
    command = attendance_find_command(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(command["filter"], hint=command["hint"])
    docs = db.attendance_logs.find(command["filter"], command["projection"]).hint(command["hint"]).sort(list(command["sort"].items())).skip(command["skip"]).limit(command["limit"])
    return {"items": [attendance_output(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out", tags=["attendance"], operation_id="punchOut", summary="Record a punch-out",
          responses={200: {"model": AttendanceOut}, 404: NOT_FOUND, 409: CONFLICT, 503: UNAVAILABLE})
def punch_out(body: PunchOutIn):
    """Close the latest eligible punch-in, including overnight shifts. The duration must be positive and at most 24 hours."""
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
    truncate_punches(state)
    validate_punch_pair(state)
    state.update(derived_fields(state, employee, doc["date"]))
    updated = db.attendance_logs.find_one_and_update(snapshot_filter(doc), {"$set": state}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "Attendance changed concurrently; retry")
    return attendance_output(updated)


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"], operation_id="regularizeAttendance",
           summary="Correct a day's attendance",
           responses={200: {"model": AttendanceOut}, 404: NOT_FOUND, 409: CONFLICT, 503: UNAVAILABLE})
def regularize(emp_code: str, date: DateText, body: RegularizeIn):
    """Omitted fields stay unchanged. Recompute derived values and append one audit entry; a no-op returns 422."""
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
    truncate_punches(after)
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


# Analytics builders are also used by the explain endpoint.
def month_bounds(month):
    year, number = map(int, month.split("-"))
    return month + "-01", f"{month}-{calendar.monthrange(year, number)[1]:02d}"


def calendar_instant(day):
    # UTC midnight is a placeholder for a calendar label, not a punch instant.
    return datetime.combine(date.fromisoformat(day), time.min, UTC)


def mongo_half_up(value, places=2):
    scale = 10 ** places
    return {"$cond": [
        {"$eq": [value, None]}, None,
        {"$divide": [{"$floor": {"$add": [
            {"$multiply": [{"$toDecimal": value}, scale]}, Decimal128("0.5"),
        ]}}, scale]},
    ]}


def ratio(numerator, denominator):
    return {"$cond": [{"$gt": [denominator, 0]}, {"$divide": [{"$toDecimal": numerator}, denominator]}, None]}


def present_weight():
    return {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def positive_late():
    return {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}


def weekday(value):
    return {"$in": [{"$dayOfWeek": value}, [2, 3, 4, 5, 6]]}


def monthly_stats(first, last):
    has_hours = {"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    return [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [weekday({"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}), present_weight(), 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [positive_late(), 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            "hours_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$work_hours"}, Decimal128("0")]}},
            "hours_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
        }},
    ]


def stats_lookup(first, last):
    return {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "pipeline": monthly_stats(first, last), "as": "stats"}}


def stats_value(key):
    return {"$ifNull": [{"$arrayElemAt": ["$stats." + key, 0]}, 0]}


def employee_monthly_pipeline(emp_code, month):
    first, last = month_bounds(month)
    day = {"$dateAdd": {"startDate": calendar_instant(first), "unit": "day", "amount": "$$offset"}}
    working_days = {"$size": {"$filter": {
        "input": {"$range": [0, int(last[-2:])]}, "as": "offset",
        "cond": {"$and": [weekday(day), {"$gte": [{"$dateToString": {"date": day, "format": "%Y-%m-%d"}}, "$joined_on"]}]},
    }}}
    return [
        {"$match": {"emp_code": emp_code}}, stats_lookup(first, last),
        {"$project": {
            "_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": working_days,
            **{key: stats_value(key) for key in ("present_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes")},
        }},
        {"$set": {"attendance_pct": mongo_half_up({"$cond": [
            {"$gt": ["$working_days", 0]}, {"$multiply": [ratio("$present_days", "$working_days"), 100]}, None,
        ]})}},
    ]


def department_summary_pipeline(month, department=None):
    first, last = month_bounds(month)
    query = {"joined_on": {"$lte": last}}
    if department is not None:
        query["department"] = department
    fields = ("present_days", "late_count", "total_late_minutes", "on_duty_count", "hours_sum", "hours_count")
    return [
        {"$match": query}, stats_lookup(first, last),
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "leave_count": {"$sum": stats_value("leave_days")},
                     **{key: {"$sum": stats_value(key)} for key in fields}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
                      "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
                      "avg_work_hours": mongo_half_up(ratio("$hours_sum", "$hours_count"))}},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month, limit=10, department=None):
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": {"$cond": [positive_late(), 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "rank": 1, "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


def trend_range(first, last):
    days = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
    if days < 1 or days > 92:
        invalid("Trend range must contain between 1 and 92 days", "query")
    return days


def department_trend_pipeline(department, first, last):
    days = trend_range(first, last)
    return [
        {"$match": {"department": department}},
        {"$facet": {
            "joins": [{"$group": {"_id": "$joined_on", "count": {"$sum": 1}}}],
            "daily": [
                {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {"$group": {"_id": "$date", "present_count": {"$sum": present_weight()}, "late_count": {"$sum": {"$cond": [positive_late(), 1, 0]}}}},
                ], "as": "days"}},
                {"$unwind": "$days"},
                {"$group": {"_id": "$days._id", "present_count": {"$sum": "$days.present_count"}, "late_count": {"$sum": "$days.late_count"}}},
            ],
            "calendar": [{"$limit": 1}, {"$project": {"_id": 0, "days": {"$map": {
                "input": {"$range": [0, days]}, "as": "offset",
                "in": {"$dateAdd": {"startDate": calendar_instant(first), "unit": "day", "amount": "$$offset"}},
            }}}}],
        }},
        {"$set": {"day": {"$ifNull": [{"$arrayElemAt": ["$calendar.days", 0]}, []]}}},
        {"$unwind": "$day"},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}}, "is_working_day": weekday("$day")}},
        {"$set": {
            "headcount": {"$sum": {"$map": {"input": {"$filter": {"input": "$joins", "as": "join", "cond": {"$lte": ["$$join._id", "$date"]}}}, "as": "join", "in": "$$join.count"}}},
            "stats": {"$arrayElemAt": [{"$filter": {"input": "$daily", "as": "row", "cond": {"$eq": ["$$row._id", "$date"]}}}, 0]},
        }},
        {"$set": {"present_count": {"$ifNull": ["$stats.present_count", 0]}, "late_count": {"$ifNull": ["$stats.late_count", 0]}}},
        {"$set": {"attendance_rate": {"$cond": ["$is_working_day", mongo_half_up(ratio("$present_count", "$headcount"), 4), None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1,
                      "moving_avg_7d": mongo_half_up("$moving_avg_7d", 4)}},
        {"$sort": {"date": 1}},
    ]


def aggregation_operation(endpoint, *, emp_code=None, month=None, department=None, limit=10, first=None, last=None):
    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            invalid("employee_monthly requires emp_code and month", "query")
        return "employees", employee_monthly_pipeline(emp_code, month), "employee_code"
    if endpoint == "department_summary":
        if month is None:
            invalid("department_summary requires month", "query")
        hint = "employee_join_department" if department is None else "employee_department_join"
        return "employees", department_summary_pipeline(month, department), hint
    if endpoint == "late_leaderboard":
        if month is None:
            invalid("late_leaderboard requires month", "query")
        return "attendance_logs", late_leaderboard_pipeline(month, limit, department), "attendance_date_employee"
    if department is None or first is None or last is None:
        invalid("department_trend requires department, from and to", "query")
    return "employees", department_trend_pipeline(department, first, last), "employee_department_join"


def aggregate(endpoint, **parameters):
    collection, pipeline, hint = aggregation_operation(endpoint, **parameters)
    return list(db[collection].aggregate(pipeline, hint=hint))


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"], operation_id="employeeMonthly",
         summary="Get an employee's monthly summary",
         responses={200: {"model": EmployeeMonthlyOut}, 404: NOT_FOUND, 503: UNAVAILABLE})
def employee_monthly(emp_code: str, month: MonthText):
    """Use YYYY-MM. Weekday presence includes half-day weights; late and overtime totals include weekends."""
    rows = aggregate("employee_monthly", emp_code=emp_code, month=month)
    if not rows:
        raise HTTPException(404, "Employee not found")
    return json_value(rows[0])


@app.get("/analytics/departments/summary", tags=["analytics"], operation_id="departmentSummary",
         summary="Get monthly department summaries", responses={200: {"model": DepartmentSummaryOut}, 503: UNAVAILABLE})
def department_summary(month: MonthText, department: str | None = None):
    """Include eligible employees without logs. Average work hours over completed presence records, not employee averages."""
    return {"month": month, "items": json_value(aggregate("department_summary", month=month, department=department))}


@app.get("/analytics/leaderboard/late", tags=["analytics"], operation_id="lateLeaderboard",
         summary="Rank employees by late minutes", responses={200: {"model": LeaderboardOut}, 503: UNAVAILABLE})
def late_leaderboard(month: MonthText, limit: Limit = 10, department: str | None = None):
    """Competition ranks are 1, 2, 2, 4. Return rank <= limit, including cutoff ties; filter department before ranking."""
    return {"month": month, "items": json_value(aggregate("late_leaderboard", month=month, limit=limit, department=department))}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"], operation_id="departmentTrend",
         summary="Get a department's daily trend", responses={200: {"model": TrendOut}, 404: NOT_FOUND, 503: UNAVAILABLE})
def department_trend(department: str, first: Annotated[DateText, Query(alias="from")], last: Annotated[DateText, Query(alias="to")]):
    """Request up to 92 inclusive dates. Include gaps and weekends; the seven-day average excludes null rates."""
    rows = aggregate("department_trend", department=department, first=first, last=last)
    if not rows:
        raise HTTPException(404, "Department not found")
    return {"department": department, "items": json_value(rows)}


ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}", tags=["admin"], operation_id="explainEndpoint", summary="Inspect a MongoDB execution plan",
         responses={200: {"model": ExplainOut}, 503: UNAVAILABLE})
def explain_endpoint(
    endpoint: ExplainEndpoint, emp_code: str | None = None, month: MonthText | None = None,
    department: str | None = None, limit: Limit = 10,
    date_from: DateText | None = None, date_to: DateText | None = None, status: Status | None = None,
    first: Annotated[DateText | None, Query(alias="from")] = None,
    last: Annotated[DateText | None, Query(alias="to")] = None,
    page: Page = 1, page_size: PageSize = 20,
):
    """Use the chosen endpoint's filters. Return its actual query/pipeline executionStats; BSON metadata uses Extended JSON."""
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = attendance_find_command(emp_code, date_from, date_to, status, page, page_size)
    else:
        collection, pipeline, hint = aggregation_operation(
            endpoint, emp_code=emp_code, month=month, department=department, limit=limit, first=first, last=last,
        )
        command = {"aggregate": collection, "pipeline": pipeline, "hint": hint, "cursor": {}}
    explanation = db.command({"explain": command, "verbosity": "executionStats"})
    # Replica-set responses include BSON timestamps and binary signatures.
    safe_explanation = json.loads(json_util.dumps(explanation, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": safe_explanation}
