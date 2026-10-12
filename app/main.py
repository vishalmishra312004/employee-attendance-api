"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (load_dotenv does not override existing env vars)
"""
from __future__ import annotations

import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from bson import json_util
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv(override=False)

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = frozenset({"PRESENT", "WFH", "ON_DUTY"})
PUNCH_IN_STATUSES = frozenset({"PRESENT", "WFH", "ON_DUTY"})
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000
EMP_CODE_RE = re.compile(r"^EMP\d{4,6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SHIFT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

client: MongoClient
db: Any


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global client, db
    client = MongoClient(MONGO_URI, tz_aware=True)
    db = client[MONGO_DB]
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique"
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp_code")
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)], name="date_emp_code_asc")
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time & business rules
# --------------------------------------------------------------------------- #
def truncate_ms(ms: int) -> int:
    return (ms // 1000) * 1000


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(truncate_ms(ms) / 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return truncate_ms(int(dt.timestamp() * 1000))


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ist_wall(dt: datetime) -> datetime:
    return ensure_utc(dt).astimezone(IST)


def parse_hhmm(s: str) -> tuple[int, int]:
    h, m = map(int, s.split(":"))
    return h, m


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def ist_date_from_dt(dt: datetime) -> date:
    return ist_wall(dt).date()


def attendance_date_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    punch_in = ensure_utc(punch_in).replace(microsecond=0)
    ist = ist_wall(punch_in)
    d = ist.date()
    if is_overnight(shift_start, shift_end):
        eh, em = parse_hhmm(shift_end)
        if ist.time() < time(eh, em):
            d -= timedelta(days=1)
    return d.isoformat()


def shift_start_utc(attendance_date_str: str, shift_start: str) -> datetime:
    y, m, d = map(int, attendance_date_str.split("-"))
    h, mi = parse_hhmm(shift_start)
    return datetime(y, m, d, h, mi, 0, tzinfo=IST).astimezone(timezone.utc)


def shift_end_utc(attendance_date_str: str, shift_start: str, shift_end: str) -> datetime:
    y, m, d = map(int, attendance_date_str.split("-"))
    h, mi = parse_hhmm(shift_end)
    if is_overnight(shift_start, shift_end):
        base = date(y, m, d) + timedelta(days=1)
        y, m, d = base.year, base.month, base.day
    return datetime(y, m, d, h, mi, 0, tzinfo=IST).astimezone(timezone.utc)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date_str: str) -> int:
    punch_in = ensure_utc(punch_in).replace(microsecond=0)
    start = shift_start_utc(attendance_date_str, shift_start)
    delta_sec = (punch_in - start).total_seconds()
    if delta_sec <= 600:
        return 0
    return int(delta_sec // 60)


def round_half_up(value: float, places: int) -> float:
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    punch_in = ensure_utc(punch_in).replace(microsecond=0)
    punch_out = ensure_utc(punch_out).replace(microsecond=0)
    hours = (punch_out - punch_in).total_seconds() / 3600
    return round_half_up(hours, 2)


def compute_overtime_minutes(
    punch_out: datetime, shift_start: str, shift_end: str, attendance_date_str: str
) -> int:
    punch_out = ensure_utc(punch_out).replace(microsecond=0)
    end = shift_end_utc(attendance_date_str, shift_start, shift_end)
    mins = int((punch_out - end).total_seconds() // 60)
    return mins if mins >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50


def is_weekday(date_str: str) -> bool:
    y, m, d = map(int, date_str.split("-"))
    return date(y, m, d).weekday() < 5


def month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def count_working_days(month: str, joined_on: str) -> int:
    start_s, end_s = month_bounds(month)
    start = max(date.fromisoformat(start_s), date.fromisoformat(joined_on))
    end = date.fromisoformat(end_s)
    if start > end:
        return 0
    n = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


def present_day_weight(rec: dict) -> float:
    if rec.get("status") not in PRESENCE:
        return 0.0
    if not is_weekday(rec["date"]):
        return 0.0
    return 0.5 if rec.get("half_day") else 1.0


def validate_epoch_ms(v: Optional[int], field: str) -> Optional[int]:
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool):
        raise HTTPException(422, f"invalid {field}")
    if v < EPOCH_MIN or v > EPOCH_MAX:
        raise HTTPException(422, f"invalid {field}")
    return truncate_ms(v)


def validate_calendar_date(value: str, field: str = "date") -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"invalid {field}")
    if parsed.isoformat() != value:
        raise HTTPException(422, f"invalid {field}")
    return value


def serialize_explain(explain: dict) -> dict:
    return json.loads(json_util.dumps(explain))


def punch_on_attendance_date(punch_in: datetime, record_date: str, shift_start: str, shift_end: str) -> bool:
    return attendance_date_for_punch(punch_in, shift_start, shift_end) == record_date


def recompute_derived(
    rec: dict, emp: dict
) -> tuple[int, Optional[float], int, bool]:
    status = rec["status"]
    punch_in = rec.get("punch_in")
    punch_out = rec.get("punch_out")
    if status in ("ABSENT", "LEAVE"):
        return 0, None, 0, False
    if punch_in is None:
        return 0, None, 0, False
    att_date = rec["date"]
    late = compute_late_minutes(punch_in, emp["shift_start"], att_date)
    if punch_out is None:
        return late, None, 0, False
    wh = compute_work_hours(punch_in, punch_out)
    ot = compute_overtime_minutes(punch_out, emp["shift_start"], emp["shift_end"], att_date)
    hd = compute_half_day(wh)
    return late, wh, ot, hd


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def serialize_history_entry(entry: dict) -> dict:
    changes: dict[str, Any] = {}
    for k, v in entry.get("changes", {}).items():
        fr, to = v.get("from"), v.get("to")
        if k in ("punch_in", "punch_out"):
            changes[k] = {
                "from": dt_to_ms(fr) if fr is not None else None,
                "to": dt_to_ms(to) if to is not None else None,
            }
        else:
            changes[k] = {"from": fr, "to": to}
    return {
        "at": dt_to_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes,
    }


def serialize_attendance(doc: dict) -> dict:
    hist = doc.get("history") or []
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc["punch_in"]) if doc.get("punch_in") else None,
        "punch_out": dt_to_ms(doc["punch_out"]) if doc.get("punch_out") else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(h) for h in hist],
    }


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def emp_code_fmt(cls, v: str) -> str:
        if not EMP_CODE_RE.match(v):
            raise ValueError("invalid emp_code")
        return v

    @field_validator("email")
    @classmethod
    def email_fmt(cls, v: str) -> str:
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def shift_fmt(cls, v: str) -> str:
        if not SHIFT_RE.match(v):
            raise ValueError("invalid shift time")
        return v

    @field_validator("joined_on")
    @classmethod
    def joined_date_fmt(cls, v: str) -> str:
        try:
            parsed = date.fromisoformat(v)
        except ValueError:
            raise ValueError("invalid joined_on")
        if parsed.isoformat() != v:
            raise ValueError("invalid joined_on")
        return v

    @model_validator(mode="after")
    def shifts_differ(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def status_ok(cls, v: str) -> str:
        if v not in PUNCH_IN_STATUSES:
            raise ValueError("invalid status")
        return v


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
# Query builders (shared with explain)
# --------------------------------------------------------------------------- #
def attendance_list_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict:
    if date_from:
        validate_calendar_date(date_from, "date_from")
    if date_to:
        validate_calendar_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    start, end = month_bounds(month)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start, "$lte": end},
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
                                    {"$in": ["$status", list(PRESENCE)]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date",
                                                        "timezone": "+05:30",
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$half_day", False]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list[dict]:
    start, end = month_bounds(month)
    month_end = end
    emp_match: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$ec"]},
                                    {"$gte": ["$date", start]},
                                    {"$lte": ["$date", end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$logs.status", list(PRESENCE)]},
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$logs.date",
                                                                "timezone": "+05:30",
                                                            }
                                                        }
                                                    },
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$ifNull": ["$logs.half_day", False]},
                                            0.5,
                                            1.0,
                                        ]
                                    },
                                    0,
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
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$ne": ["$logs.work_hours", None]},
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
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$ne": ["$logs.work_hours", None]},
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
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": ["$logs.late_minutes", 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list[dict]:
    start, end = month_bounds(month)
    match_logs: dict[str, Any] = {"date": {"$gte": start, "$lte": end}}
    pipeline: list[dict] = [
        {"$match": match_logs},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.extend(
        [
            {
                "$project": {
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": 1,
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def department_trend_pipeline(department: str, from_date: str, to_date: str) -> list[dict]:
    from_iso = {"$dateFromString": {"dateString": from_date, "timezone": "+05:30"}}
    to_iso = {"$dateFromString": {"dateString": to_date, "timezone": "+05:30"}}
    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {"$push": {"joined_on": "$joined_on", "emp_code": "$emp_code"}},
            }
        },
        {
            "$project": {
                "employees": 1,
                "day_count": {
                    "$add": [
                        {
                            "$dateDiff": {
                                "startDate": from_iso,
                                "endDate": to_iso,
                                "unit": "day",
                            }
                        },
                        1,
                    ]
                },
                "from_dt": from_iso,
            }
        },
        {
            "$project": {
                "employees": 1,
                "dates": {
                    "$map": {
                        "input": {"$range": [0, "$day_count"]},
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": "$from_dt",
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                                "timezone": "+05:30",
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$dates"},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "d": "$dates",
                    "codes": {"$map": {"input": "$employees", "as": "e", "in": "$$e.emp_code"}},
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$d"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {"$unwind": "$emp"},
                    {"$match": {"emp.department": department}},
                ],
                "as": "day_logs",
            }
        },
        {
            "$project": {
                "date": "$dates",
                "employees": 1,
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$dates"]},
                        }
                    }
                },
                "is_working_day": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$dates",
                                    "timezone": "+05:30",
                                }
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "lg",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$lg.status", list(PRESENCE)]},
                                    {
                                        "$cond": [
                                            {"$ifNull": ["$$lg.half_day", False]},
                                            0.5,
                                            1.0,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "lg",
                            "in": {"$cond": [{"$gt": ["$$lg.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
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
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "rate_window": {
                        "$push": "$attendance_rate",
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
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$let": {
                        "vars": {
                            "valid": {
                                "$filter": {
                                    "input": "$rate_window",
                                    "as": "r",
                                    "cond": {"$ne": ["$$r", None]},
                                }
                            }
                        },
                        "in": {
                            "$cond": [
                                {"$gt": [{"$size": "$$valid"}, 0]},
                                {"$avg": "$$valid"},
                                None,
                            ]
                        },
                    }
                },
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = [
        serialize_employee(d)
        for d in db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "employee not found")
    punched_ms = (
        validate_epoch_ms(body.punched_at, "punched_at")
        if body.punched_at is not None
        else dt_to_ms(datetime.now(timezone.utc))
    )
    ts = ms_to_dt(punched_ms)
    att_date = attendance_date_for_punch(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], att_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "employee not found")
    punched_ms = (
        validate_epoch_ms(body.punched_at, "punched_at")
        if body.punched_at is not None
        else dt_to_ms(datetime.now(timezone.utc))
    )
    punched_at = ms_to_dt(punched_ms)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no open punch-in found")
    punch_in = ensure_utc(rec["punch_in"]).replace(microsecond=0)
    if punched_at <= punch_in:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (punched_at - punch_in).total_seconds() > 86400:
        raise HTTPException(422, "punched_at too far after punch_in")
    wh = compute_work_hours(punch_in, punched_at)
    ot = compute_overtime_minutes(punched_at, emp["shift_start"], emp["shift_end"], rec["date"])
    hd = compute_half_day(wh)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_at, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    validate_calendar_date(date)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "attendance record not found")

    if body.status is not None and body.status not in PRESENCE.union({"ABSENT", "LEAVE"}):
        raise HTTPException(422, "invalid status")
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "punch times not allowed with ABSENT or LEAVE")

    new_status = body.status if body.status is not None else rec["status"]
    new_pin = rec.get("punch_in")
    new_pout = rec.get("punch_out")
    if body.punch_in is not None:
        new_pin = ms_to_dt(validate_epoch_ms(body.punch_in, "punch_in"))
    if body.punch_out is not None:
        new_pout = ms_to_dt(validate_epoch_ms(body.punch_out, "punch_out"))

    if new_status in PRESENCE:
        if new_pin is None:
            raise HTTPException(422, "presence status requires punch_in")
        if not punch_on_attendance_date(new_pin, date, emp["shift_start"], emp["shift_end"]):
            raise HTTPException(422, "punch_in must be on attendance date")
        if new_pout is not None:
            pin = ensure_utc(new_pin).replace(microsecond=0)
            pout = ensure_utc(new_pout).replace(microsecond=0)
            if pout <= pin:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (pout - pin).total_seconds() > 86400:
                raise HTTPException(422, "punch_out too far after punch_in")
    else:
        new_pin = None
        new_pout = None

    candidate = {
        "status": new_status,
        "punch_in": new_pin,
        "punch_out": new_pout,
        "date": date,
    }
    late, wh, ot, hd = recompute_derived(candidate, emp)

    before = serialize_attendance(rec)
    after_doc = {
        **rec,
        "status": new_status,
        "punch_in": new_pin,
        "punch_out": new_pout,
        "late_minutes": late,
        "work_hours": wh,
        "overtime_minutes": ot,
        "half_day": hd,
    }
    after = serialize_attendance(after_doc)

    changes: dict[str, Any] = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if before[field] != after[field]:
            changes[field] = {"from": before[field], "to": after[field]}
    if not changes:
        raise HTTPException(422, "no changes")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {},
    }
    for k, v in changes.items():
        if k in ("punch_in", "punch_out"):
            history_entry["changes"][k] = {
                "from": ms_to_dt(v["from"]) if v["from"] is not None else None,
                "to": ms_to_dt(v["to"]) if v["to"] is not None else None,
            }
        else:
            history_entry["changes"][k] = v

    hist_len = len(rec.get("history") or [])
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, hist_len]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pin,
                "punch_out": new_pout,
                "late_minutes": late,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(409, "concurrent regularization")
    return serialize_attendance(result)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    if not MONTH_RE.match(month):
        raise HTTPException(422, "invalid month")
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "employee not found")
    working_days = count_working_days(month, emp["joined_on"])
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    agg = rows[0] if rows else {}
    present_days = float(agg.get("present_days") or 0)
    leave_days = int(agg.get("leave_days") or 0)
    late_count = int(agg.get("late_count") or 0)
    total_late = int(agg.get("total_late_minutes") or 0)
    total_ot = int(agg.get("total_overtime_minutes") or 0)
    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    if not MONTH_RE.match(month):
        raise HTTPException(422, "invalid month")
    items = []
    for row in db.employees.aggregate(department_summary_pipeline(month, department)):
        avg_wh = row.get("avg_work_hours")
        items.append(
            {
                "department": row["department"],
                "headcount": int(row["headcount"]),
                "present_days": float(row.get("present_days") or 0),
                "avg_work_hours": round_half_up(avg_wh, 2) if avg_wh is not None else None,
                "late_count": int(row.get("late_count") or 0),
                "total_late_minutes": int(row.get("total_late_minutes") or 0),
                "leave_count": int(row.get("leave_count") or 0),
                "on_duty_count": int(row.get("on_duty_count") or 0),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    if not MONTH_RE.match(month):
        raise HTTPException(422, "invalid month")
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    validate_calendar_date(from_date, "from")
    validate_calendar_date(to_date, "to")
    if to_date < from_date:
        raise HTTPException(422, "invalid date range")
    d0 = date.fromisoformat(from_date)
    d1 = date.fromisoformat(to_date)
    if (d1 - d0).days > 91:
        raise HTTPException(422, "date range too large")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")
    raw = list(db.employees.aggregate(department_trend_pipeline(department, from_date, to_date)))
    items = []
    for row in raw:
        rate = row.get("attendance_rate")
        mv = row.get("moving_avg_7d")
        items.append(
            {
                "date": row["date"],
                "is_working_day": bool(row["is_working_day"]),
                "headcount": int(row["headcount"]),
                "present_count": float(row.get("present_count") or 0),
                "late_count": int(row.get("late_count") or 0),
                "attendance_rate": round_half_up(rate, 4) if rate is not None else None,
                "moving_avg_7d": round_half_up(mv, 4) if mv is not None else None,
            }
        )
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = attendance_list_query(emp_code, date_from, date_to, status)
        skip = (page - 1) * page_size
        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": DESCENDING, "emp_code": ASCENDING},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": serialize_explain(explain)}
    if endpoint == "employee_monthly":
        if not emp_code or not month or not MONTH_RE.match(month):
            raise HTTPException(422, "missing parameters")
        pipeline = employee_monthly_pipeline(emp_code, month)
        explain = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": serialize_explain(explain)}
    if endpoint == "department_summary":
        if not month or not MONTH_RE.match(month):
            raise HTTPException(422, "missing parameters")
        pipeline = department_summary_pipeline(month, department)
        explain = db.employees.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "employees", "explain": serialize_explain(explain)}
    if endpoint == "late_leaderboard":
        if not month or not MONTH_RE.match(month):
            raise HTTPException(422, "missing parameters")
        pipeline = late_leaderboard_pipeline(month, limit, department)
        explain = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": serialize_explain(explain)}
    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "missing parameters")
        pipeline = department_trend_pipeline(department, from_date, to_date)
        explain = db.employees.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "employees", "explain": serialize_explain(explain)}
    raise HTTPException(422, "unknown endpoint")
