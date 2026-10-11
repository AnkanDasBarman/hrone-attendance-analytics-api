"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import AsyncGenerator, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import (
    BaseModel,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


def create_indexes():
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employee_code",
    )
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="ix_employee_department_joined",
    )
    db.employees.create_index(
        [("joined_on", 1)],
        name="ix_employee_joined_on",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)],
        name="ix_attendance_employee_punchin",
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_date_employee",
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def as_utc(value: datetime) -> datetime:
    """Normalize a datetime to a whole-second UTC instant."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def attendance_date_for(punched_at: datetime, shift_start: str, shift_end: str) -> str:
    """Return the IST attendance date, assigning early overnight punches to yesterday."""
    local_time = as_utc(punched_at).astimezone(IST)
    start = tuple(map(int, shift_start.split(":")))
    end = tuple(map(int, shift_end.split(":")))
    date = local_time.date()
    if end <= start and (local_time.hour, local_time.minute) < end:
        date -= timedelta(days=1)
    return date.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    local_in = as_utc(punch_in).astimezone(IST)
    hour, minute = map(int, shift_start.split(":"))
    start = datetime.fromisoformat(attendance_date).replace(
        hour=hour, minute=minute, second=0, microsecond=0, tzinfo=IST
    )
    elapsed_seconds = (local_in - start).total_seconds()
    minutes = int(elapsed_seconds // 60)
    return minutes if elapsed_seconds > 10 * 60 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (as_utc(punch_out) - as_utc(punch_in)).total_seconds()
    hours = Decimal(str(seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(attendance_date).replace(
        hour=end_hour,
        minute=end_minute,
        second=0,
        microsecond=0,
        tzinfo=IST,
    )
    if (end_hour, end_minute) <= (start_hour, start_minute):
        end += timedelta(days=1)
    minutes = int((as_utc(punch_out).astimezone(IST) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def to_epoch_ms(value):
    """Convert a MongoDB BSON datetime (naive UTC) to epoch milliseconds."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return int(value.timestamp() * 1000)


def serialize_attendance(doc):
    """Return an attendance document in the API's required format."""
    history = []
    for entry in doc.get("history", []):
        changes = {}
        for field, change in entry.get("changes", {}).items():
            changes[field] = {
                "from": change.get("from"),
                "to": change.get("to"),
            }
            if field in ("punch_in", "punch_out"):
                changes[field]["from"] = to_epoch_ms(change.get("from"))
                changes[field]["to"] = to_epoch_ms(change.get("to"))
        history.append(
            {
                "at": to_epoch_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def epoch_ms_to_datetime(value: int) -> datetime:
    """Convert epoch milliseconds into a whole-second UTC datetime."""
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(
        microsecond=0
    )


def compute_half_day(work_hours: Optional[float]) -> bool:
    return work_hours is not None and work_hours < 4.50


def validate_epoch_ms(value: Optional[int]) -> None:
    if value is not None and not (100_000_000_000 <= value <= 4_102_444_800_000):
        raise HTTPException(422, "Timestamp must be epoch milliseconds")


def round_half_up(value, places=2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date") from exc
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        ) from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    doc.pop("_id", None)
    doc["created_at"] = to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {}
    if department:
        query["department"] = department

    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    for item in items:
        if item.get("created_at") is not None:
            item["created_at"] = to_epoch_ms(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    validate_epoch_ms(body.punched_at)
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Unknown employee")

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(422, "Invalid punch-in status")

    ts = (
        datetime.now(timezone.utc).replace(microsecond=0)
        if body.punched_at is None
        else datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc).replace(
            microsecond=0
        )
    )
    attendance_date = attendance_date_for(
        ts, emp["shift_start"], emp["shift_end"]
    )
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts, emp["shift_start"], attendance_date
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409,
            detail="Attendance record already exists",
        ) from exc
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    validate_epoch_ms(body.punched_at)
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Unknown employee")

    ts = (
        as_utc(datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc))
        if body.punched_at is not None
        else as_utc(datetime.now(timezone.utc))
    )
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
            "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
        },
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Attendance record already punched out")

    punch_in_time = as_utc(record["punch_in"])
    elapsed_seconds = (ts - punch_in_time).total_seconds()
    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            422, "Punch-out must be after punch-in and within 24 hours"
        )

    work_hours = compute_work_hours(punch_in_time, ts)
    overtime = compute_overtime(
        ts, emp["shift_start"], emp["shift_end"], record["date"]
    )
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": work_hours < 4.50,
            }
        },
    )
    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record already punched out")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        parsed_date = datetime.fromisoformat(date).date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "date must use YYYY-MM-DD format")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Unknown employee")
    original = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not original:
        raise HTTPException(404, "Attendance record not found")

    supplied = body.model_fields_set
    if "status" in supplied and body.status is None:
        raise HTTPException(422, "status cannot be null")
    for field in ("punch_in", "punch_out"):
        if field in supplied and getattr(body, field) is None:
            raise HTTPException(422, f"{field} cannot be null")
        validate_epoch_ms(getattr(body, field))

    statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
    if body.status is not None and body.status not in statuses:
        raise HTTPException(422, "Invalid attendance status")
    if body.status in {"ABSENT", "LEAVE"} and (
        "punch_in" in supplied or "punch_out" in supplied
    ):
        raise HTTPException(422, "ABSENT or LEAVE cannot include punch times")

    old = {
        "status": original["status"],
        "punch_in": (
            as_utc(original["punch_in"])
            if original.get("punch_in") is not None
            else None
        ),
        "punch_out": (
            as_utc(original["punch_out"])
            if original.get("punch_out") is not None
            else None
        ),
        "work_hours": original.get("work_hours"),
        "late_minutes": original.get("late_minutes", 0),
        "overtime_minutes": original.get("overtime_minutes", 0),
        "half_day": original.get("half_day", False),
    }
    final = dict(old)
    if body.status is not None:
        final["status"] = body.status
    if "punch_in" in supplied:
        if body.punch_in is None:
            raise HTTPException(422, "punch_in cannot be null")
        final["punch_in"] = epoch_ms_to_datetime(body.punch_in)
    if "punch_out" in supplied:
        if body.punch_out is None:
            raise HTTPException(422, "punch_out cannot be null")
        final["punch_out"] = epoch_ms_to_datetime(body.punch_out)

    is_non_presence_status = final["status"] in {"ABSENT", "LEAVE"}
    if is_non_presence_status:
        final["punch_in"] = None
        final["punch_out"] = None
    elif final["punch_in"] is None:
        raise HTTPException(422, "A presence status requires punch_in")

    if is_non_presence_status:
        final.update(
            work_hours=None,
            late_minutes=0,
            overtime_minutes=0,
            half_day=False,
        )
    else:
        punch_in_time = final["punch_in"]
        if punch_in_time is None:
            raise HTTPException(422, "A presence status requires punch_in")

        expected_date = attendance_date_for(
            punch_in_time, emp["shift_start"], emp["shift_end"]
        )
        if expected_date != date:
            raise HTTPException(422, "punch_in must belong to the attendance date")

        late_minutes = compute_late_minutes(
            punch_in_time, emp["shift_start"], date
        )
        punch_out_time = final["punch_out"]
        if punch_out_time is None:
            final.update(
                work_hours=None,
                late_minutes=late_minutes,
                overtime_minutes=0,
                half_day=False,
            )
        else:
            elapsed_seconds = (punch_out_time - punch_in_time).total_seconds()
            if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
                raise HTTPException(
                    422, "punch_out must be after punch_in and within 24 hours"
                )
            work_hours = compute_work_hours(punch_in_time, punch_out_time)
            final.update(
                work_hours=work_hours,
                late_minutes=late_minutes,
                overtime_minutes=compute_overtime(
                    punch_out_time,
                    emp["shift_start"],
                    emp["shift_end"],
                    date,
                ),
                half_day=compute_half_day(work_hours),
            )

    tracked_fields = (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    )
    changes = {
        field: {"from": old[field], "to": final[field]}
        for field in tracked_fields
        if old[field] != final[field]
    }
    if not changes:
        raise HTTPException(422, "Regularization changes nothing")

    history_entry = {
        "at": as_utc(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    compare_and_swap = {"_id": original["_id"]}
    for field in (*tracked_fields, "history"):
        if field in original:
            compare_and_swap[field] = original[field]
        else:
            compare_and_swap[field] = {"$exists": False}

    result = db.attendance_logs.update_one(
        compare_and_swap,
        {
            "$set": {field: final[field] for field in tracked_fields},
            "$push": {"history": history_entry},
        },
    )
    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record was modified concurrently")

    updated = db.attendance_logs.find_one({"_id": original["_id"]})
    return serialize_attendance(updated)


def parse_calendar_date(
    value: Optional[str],
    field_name: str,
) -> Optional[date]:
    """Validate a real YYYY-MM-DD calendar date."""
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be a valid YYYY-MM-DD date",
        ) from exc
    if parsed.isoformat() != value:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be a valid YYYY-MM-DD date",
        )
    return parsed


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    parsed_date_from = parse_calendar_date(date_from, "date_from")
    parsed_date_to = parse_calendar_date(date_to, "date_to")
    if (
        parsed_date_from is not None
        and parsed_date_to is not None
        and parsed_date_from > parsed_date_to
    ):
        raise HTTPException(422, "date_from must be <= date_to")

    valid_statuses = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
    if status is not None and status not in valid_statuses:
        raise HTTPException(422, "Invalid attendance status")

    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if parsed_date_from or parsed_date_to:
        query["date"] = {}
        if parsed_date_from:
            query["date"]["$gte"] = parsed_date_from.isoformat()
        if parsed_date_to:
            query["date"]["$lte"] = parsed_date_to.isoformat()
    if status:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [serialize_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def build_employee_monthly_pipeline(
    emp_code: str,
    month_start: str,
    month_end: str,
):
    is_weekday = {
        "$and": [
            {
                "$gte": [
                    {
                        "$dayOfWeek": {
                            "$dateFromString": {"dateString": "$date"}
                        }
                    },
                    2,
                ]
            },
            {
                "$lte": [
                    {
                        "$dayOfWeek": {
                            "$dateFromString": {"dateString": "$date"}
                        }
                    },
                    6,
                ]
            },
        ]
    }
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    is_weekday,
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$half_day", False]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "Unknown employee")

    year, month_number = map(int, month.split("-"))
    try:
        last_day = calendar.monthrange(year, month_number)[1]
        month_start_date = date(year, month_number, 1)
        month_end_date = date(year, month_number, last_day)
    except ValueError:
        raise HTTPException(422, "Invalid month")

    month_start = month_start_date.isoformat()
    month_end = month_end_date.isoformat()
    joined_on = date.fromisoformat(employee["joined_on"])
    effective_start = max(month_start_date, joined_on)
    working_days = 0
    if effective_start <= month_end_date:
        current_day = effective_start
        while current_day <= month_end_date:
            if current_day.weekday() < 5:
                working_days += 1
            current_day += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(
        emp_code,
        month_start,
        month_end,
    )
    aggregate = next(db.attendance_logs.aggregate(pipeline), None)
    metrics = aggregate or {}
    present_days = metrics.get("present_days", 0)
    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2)
        if working_days
        else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": metrics.get("leave_days", 0),
        "late_count": metrics.get("late_count", 0),
        "total_late_minutes": metrics.get("total_late_minutes", 0),
        "total_overtime_minutes": metrics.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(
    month_start: str,
    month_end: str,
    department: Optional[str] = None,
):
    employee_match: dict[str, object] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department

    weekday_expression = {
        "$and": [
            {
                "$gte": [
                    {
                        "$dayOfWeek": {
                            "$dateFromString": {"dateString": "$date"}
                        }
                    },
                    2,
                ]
            },
            {
                "$lte": [
                    {
                        "$dayOfWeek": {
                            "$dateFromString": {"dateString": "$date"}
                        }
                    },
                    6,
                ]
            },
        ]
    }
    presence_status = {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {"$addFields": {"_is_weekday": weekday_expression}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {"department": "$department", "emp_code": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [presence_status, "$logs._is_weekday"]},
                            {
                                "$cond": [
                                    {"$ifNull": ["$logs.half_day", False]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    presence_status,
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    presence_status,
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, month_number = map(int, month.split("-"))
    try:
        last_day = calendar.monthrange(year, month_number)[1]
        month_start = date(year, month_number, 1).isoformat()
        month_end = date(year, month_number, last_day).isoformat()
    except ValueError:
        raise HTTPException(422, "Invalid month")

    pipeline = build_department_summary_pipeline(
        month_start,
        month_end,
        department,
    )
    items = []
    for result in db.employees.aggregate(pipeline):
        average = (
            result["work_hours_sum"] / result["work_hours_count"]
            if result["work_hours_count"]
            else None
        )
        items.append(
            {
                "department": result["_id"],
                "headcount": result["headcount"],
                "present_days": result["present_days"],
                "avg_work_hours": round_half_up(average, 2),
                "late_count": result["late_count"],
                "total_late_minutes": result["total_late_minutes"],
                "leave_count": result["leave_count"],
                "on_duty_count": result["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month_start: str,
    month_end: str,
    limit: int,
    department: Optional[str] = None,
):
    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, month_number = map(int, month.split("-"))
    try:
        last_day = calendar.monthrange(year, month_number)[1]
        month_start = date(year, month_number, 1).isoformat()
        month_end = date(year, month_number, last_day).isoformat()
    except ValueError:
        raise HTTPException(422, "Invalid month")

    pipeline = build_late_leaderboard_pipeline(
        month_start=month_start,
        month_end=month_end,
        limit=limit,
        department=department,
    )
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


def build_department_trend_pipeline(
    department: str,
    start_dt: datetime,
    end_exclusive: datetime,
):
    is_working_day = {
        "$and": [
            {"$gte": [{"$dayOfWeek": "$day"}, 2]},
            {"$lte": [{"$dayOfWeek": "$day"}, 6]},
        ]
    }
    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "day": {"$literal": start_dt}}},
        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive],
                },
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "date": "$day",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"attendance_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$attendance_date"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {"$match": {"employee.department": department}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$ifNull": ["$half_day", False]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "attendance",
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"attendance_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$attendance_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$ifNull": [{"$arrayElemAt": ["$attendance.present_count", 0]}, 0]
                },
                "late_count": {
                    "$ifNull": [{"$arrayElemAt": ["$attendance.late_count", 0]}, 0]
                },
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$headcount_result.count", 0]}, 0]
                },
                "is_working_day": is_working_day,
            }
        },
        {
            "$set": {
                "attendance_rate_raw": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate_raw",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$eq": ["$attendance_rate_raw", None]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {"$multiply": ["$attendance_rate_raw", 10000]},
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_raw", None]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {"$multiply": ["$moving_avg_raw", 10000]},
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if to_date < from_date:
        raise HTTPException(422, "'to' must be on or after 'from'")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")
    if not db.employees.count_documents({"department": department}):
        raise HTTPException(404, "Department not found")

    start_dt = datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc)
    end_exclusive = datetime.combine(
        to_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    )
    pipeline = build_department_trend_pipeline(
        department=department,
        start_dt=start_dt,
        end_exclusive=end_exclusive,
    )
    return {
        "department": department,
        "items": list(db.employees.aggregate(pipeline)),
    }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(
        default=None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    from_date: Optional[date] = Query(default=None, alias="from"),
    to_date: Optional[date] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    supported_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in supported_endpoints:
        raise HTTPException(
            status_code=422,
            detail="Unsupported explain endpoint",
        )

    if endpoint == "department_trend":
        if not department or from_date is None or to_date is None:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required for department_trend",
            )
        if to_date < from_date:
            raise HTTPException(422, "'to' must be on or after 'from'")
        if (to_date - from_date).days + 1 > 92:
            raise HTTPException(422, "Date range cannot exceed 92 days")
        if not db.employees.count_documents({"department": department}):
            raise HTTPException(404, "Department not found")
        start_dt = datetime.combine(
            from_date,
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        end_exclusive = datetime.combine(
            to_date + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        pipeline = build_department_trend_pipeline(
            department=department,
            start_dt=start_dt,
            end_exclusive=end_exclusive,
        )
        explain_result = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_result,
        }

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                422,
                "emp_code and month are required for employee_monthly",
            )
        employee = db.employees.find_one({"emp_code": emp_code})
        if employee is None:
            raise HTTPException(404, "Unknown employee")
        year, month_number = map(int, month.split("-"))
        try:
            last_day = calendar.monthrange(year, month_number)[1]
            month_start = date(year, month_number, 1).isoformat()
            month_end = date(year, month_number, last_day).isoformat()
        except ValueError:
            raise HTTPException(422, "Invalid month")
        pipeline = build_employee_monthly_pipeline(
            emp_code,
            month_start,
            month_end,
        )
        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(
                422,
                "month is required for department_summary",
            )
        year, month_number = map(int, month.split("-"))
        try:
            last_day = calendar.monthrange(year, month_number)[1]
            month_start = date(year, month_number, 1).isoformat()
            month_end = date(year, month_number, last_day).isoformat()
        except ValueError:
            raise HTTPException(422, "Invalid month")
        pipeline = build_department_summary_pipeline(
            month_start,
            month_end,
            department,
        )
        explain = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain,
        }

    if endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(
                status_code=422,
                detail="month is required for late_leaderboard",
            )
        year, month_number = map(int, month.split("-"))
        try:
            last_day = calendar.monthrange(year, month_number)[1]
            month_start = date(year, month_number, 1).isoformat()
            month_end = date(year, month_number, last_day).isoformat()
        except ValueError:
            raise HTTPException(422, "Invalid month")
        pipeline = build_late_leaderboard_pipeline(
            month_start=month_start,
            month_end=month_end,
            limit=limit,
            department=department,
        )
        explain_result = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_result,
        }

    parsed_date_from = parse_calendar_date(
        date_from.isoformat() if date_from is not None else None,
        "date_from",
    )
    parsed_date_to = parse_calendar_date(
        date_to.isoformat() if date_to is not None else None,
        "date_to",
    )
    if (
        parsed_date_from is not None
        and parsed_date_to is not None
        and parsed_date_from > parsed_date_to
    ):
        raise HTTPException(422, "date_from must be <= date_to")

    valid_statuses = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
    if status is not None and status not in valid_statuses:
        raise HTTPException(422, "Invalid attendance status")

    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if parsed_date_from or parsed_date_to:
        query["date"] = {}
        if parsed_date_from:
            query["date"]["$gte"] = parsed_date_from.isoformat()
        if parsed_date_to:
            query["date"]["$lte"] = parsed_date_to.isoformat()
    if status:
        query["status"] = status

    explain_command = {
        "find": "attendance_logs",
        "filter": query,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }
    explain = db.command("explain", explain_command, verbosity="executionStats")
    return {
        "endpoint": endpoint,
        "collection": "attendance_logs",
        "explain": explain,
    }
