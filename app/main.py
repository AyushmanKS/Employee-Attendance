"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError


load_dotenv()

MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB = os.environ["MONGO_DB"]

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

GRACE_SECONDS = 10 * 60        
MIN_OVERTIME_MINUTES = 30      
HALF_DAY_HOURS = 4.5           


def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.employees.create_index("joined_on")

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()   
    yield              

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    return EPOCH + timedelta(seconds=ms // 1000)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def is_overnight(emp: dict) -> bool:
    return parse_hhmm(emp["shift_end"]) <= parse_hhmm(emp["shift_start"])


def shift_start_for(punch_in: datetime, emp: dict) -> datetime:
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        day -= timedelta(days=1)         
    return datetime.combine(day, parse_hhmm(emp["shift_start"]), tzinfo=IST)


def shift_end_for(date_str: str, emp: dict) -> datetime:
    day = date.fromisoformat(date_str)
    if is_overnight(emp):
        day += timedelta(days=1)
    return datetime.combine(day, parse_hhmm(emp["shift_end"]), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    seconds = (punch_in - shift_start).total_seconds()
    return int(seconds // 60) if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    minutes = int((punch_out - shift_end).total_seconds() // 60)
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)

def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):
            change = {"from": dt_to_ms(change.get("from")), "to": dt_to_ms(change.get("to"))}
        changes[field] = change
    return {"at": dt_to_ms(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes}

def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }

def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }

def round_half_up(value, places: int = 2) -> float:
    q = Decimal(1).scaleb(-places)           
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def count_working_days(start: date, end: date) -> int:
    if start > end:
        return 0
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def check_date(v: str) -> str:
    if not DATE_RE.match(v):
        raise ValueError("must be YYYY-MM-DD")
    date.fromisoformat(v)
    return v


def raise_422(loc: tuple, msg: str):
    raise RequestValidationError([{"loc": loc, "msg": msg, "type": "value_error"}])


DateStr = Annotated[str, AfterValidator(check_date)]

EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
PresenceStatus = Literal["PRESENT" , "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"

def weekday_expr(date_field: str) -> dict:
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": date_field}}}, [2, 3, 4, 5, 6]]}


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$addFields": {
            "is_weekday": weekday_expr("$date"),
            "is_present": {"$in": ["$status", sorted(PRESENCE_STATUSES)]},
            "late": {"$ifNull": ["$late_minutes", 0]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": ["$is_weekday", "$is_present"]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late"},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]

def department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last.isoformat()}}          
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {
            "is_present": {"$in": ["$logs.status", sorted(PRESENCE_STATUSES)]},
            "is_weekday": weekday_expr("$logs.date"),
            "late": {"$ifNull": ["$logs.late_minutes", 0]},
        }},
        {"$group": {
            "_id": "$department",
            "employees": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": ["$is_weekday", "$is_present"]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "avg_work_hours": {"$avg": {"$cond": ["$is_present", "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$employees"},
            "present_days": 1, "avg_work_hours": 1, "late_count": 1,
            "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field("18:30", pattern=HHMM_PATTERN)
    joined_on: DateStr

    @model_validator(mode="after")
    def shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None

class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q)
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [employee_out(e) for e in cursor], "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    shift_start = shift_start_for(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": shift_start.date().isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, shift_start),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if record is None or record.get("punch_out") is not None:
        if db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_out": None, "punch_in": {"$gt": ts}}):
            raise_422(("body", "punched_at"), "punched_at must be after punch_in")
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    worked = (ts - record["punch_in"]).total_seconds()
    if worked <= 0 or worked > 24 * 3600:
        raise_422(("body", "punched_at"), "punched_at must be after punch_in and within 24 hours")

    work_hours = compute_work_hours(record["punch_in"], ts)
    updates = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(ts, shift_end_for(record["date"], emp)),
        "half_day": work_hours < HALF_DAY_HOURS,
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "no record for that date")

    status = body.status or record["status"]
    if status in PRESENCE_STATUSES:
        punch_in = ms_to_dt(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        punch_out = ms_to_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")
        if punch_in is None:
            raise_422(("body", "punch_in"), "a presence status requires punch_in")
        if body.punch_in is not None and shift_start_for(punch_in, emp).date().isoformat() != date:
            raise_422(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        if punch_out is not None:
            worked = (punch_out - punch_in).total_seconds()
            if worked <= 0 or worked > 24 * 3600:
                raise_422(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours")
    else:
        if body.punch_in is not None or body.punch_out is not None:
            raise_422(("body", "status"), f"{status} cannot have punch times")
        punch_in = punch_out = None

    new = {"status": status, "punch_in": punch_in, "punch_out": punch_out,
           "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if punch_in is not None:
        new["late_minutes"] = compute_late_minutes(punch_in, shift_start_for(punch_in, emp))
    if punch_in is not None and punch_out is not None:
        new["work_hours"] = compute_work_hours(punch_in, punch_out)
        new["overtime_minutes"] = compute_overtime(punch_out, shift_end_for(date, emp))
        new["half_day"] = new["work_hours"] < HALF_DAY_HOURS

    defaults = {"punch_in": None, "punch_out": None, "work_hours": None,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    changes = {}
    for field, to in new.items():
        before = record.get(field, defaults.get(field))
        if before != to:
            changes[field] = {"from": before, "to": to}
    if not changes:
        raise_422(("body",), "the request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history_len = len(record.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "status": record["status"],
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed by another request; retry")
    return record_out(updated)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise_422(("query", "date_from"), "date_from must be on or before date_to")

    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_PATTERN)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    totals = rows[0] if rows else {}

    first, last = month_bounds(month)
    working_days = count_working_days(max(first, date.fromisoformat(emp["joined_on"])), last)
    present_days = totals.get("present_days", 0)
    attendance_pct = (
        round_half_up(Decimal(str(present_days)) * 100 / working_days) if working_days else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("total_late_minutes", 0),
        "total_overtime_minutes": totals.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_PATTERN), department: Optional[str] = None):
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"])   # R8
    return {"month": month, "items": items}

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
