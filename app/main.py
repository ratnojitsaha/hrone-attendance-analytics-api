"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import json
from bson.json_util import dumps

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
# from pydantic import BaseModel, ConfigDict, Field
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="idx_employee_department_code"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_employee"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="idx_attendance_employee_date_status"
    )


UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return truncate_seconds(dt.astimezone(UTC))


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return int(to_utc(dt).timestamp() * 1000)


def from_epoch_ms(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "Timestamp must be an integer")
    if not 100000000000 <= value <= 4102444800000:
        raise HTTPException(422, "Timestamp is out of range")
    return datetime.fromtimestamp(value / 1000, tz=UTC)





def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: Optional[str] = None,
) -> int:
    local = to_utc(punch_in).astimezone(IST)
    h, m = map(int, shift_start.split(":"))

    # Overnight early-morning punch: the shift started yesterday.
    if shift_end is not None:
        end_h, end_m = map(int, shift_end.split(":"))
        if (end_h, end_m) <= (h, m) and (
            local.hour, local.minute
        ) < (end_h, end_m):
            return 0

    start = local.replace(hour=h, minute=m, second=0, microsecond=0)
    elapsed_seconds = (local - start).total_seconds()

    if elapsed_seconds <= 600:
        return 0

    return int(elapsed_seconds // 60)



def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((to_utc(punch_out) - to_utc(punch_in)).total_seconds())
    value = Decimal(seconds) / Decimal(3600)
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime, shift_end: str, date_str: str,
    shift_start: str = "09:30"
) -> int:
    h, m = map(int, shift_end.split(":"))
    start_h, start_m = map(int, shift_start.split(":"))
    end_date = datetime.fromisoformat(date_str).date()

    # Overnight shifts end on the following calendar day.
    if (h, m) <= (start_h, start_m):
        end_date += timedelta(days=1)

    end = datetime(
        end_date.year, end_date.month, end_date.day,
        h, m, tzinfo=IST
    ).astimezone(UTC)

    minutes = int((to_utc(punch_out) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    email: str = Field(min_length=1)
    department: str = Field(min_length=1)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value):
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError:
            raise ValueError("Shift time must be HH:MM")
        if len(value) != 5:
            raise ValueError("Shift time must be HH:MM")
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("Shift start and end must differ")
        try:
            datetime.strptime(self.joined_on, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return self



class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None:
            from_epoch_ms(value)
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value not in {"PRESENT", "WFH", "ON_DUTY"}:
            raise ValueError("Invalid punch-in status")
        return value




class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None:
            from_epoch_ms(value)
        return value



class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)



# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")




@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)
    return doc



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
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }




@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = (
        from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else datetime.now(timezone.utc).replace(microsecond=0)
    )

    local_ts = ts.astimezone(IST)
    date_str = local_ts.date().isoformat()

    shift_start = datetime.strptime(emp["shift_start"], "%H:%M").time()
    shift_end = datetime.strptime(emp["shift_end"], "%H:%M").time()

    # For overnight shifts, early-morning punches belong to the previous date.
    if shift_end <= shift_start and local_ts.time().replace(tzinfo=None) < shift_end:
        date_str = (local_ts.date() - timedelta(days=1)).isoformat()


    # Prevent attendance before the employee joined.
    if date_str < emp["joined_on"]:
        raise HTTPException(400, "Attendance date is before joining date")

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts, emp["shift_start"], emp["shift_end"]
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")

    doc["id"] = str(result.inserted_id)
    doc.pop("_id", None)
    doc["punch_in"] = to_epoch_ms(doc["punch_in"])
    doc["punch_out"] = None
    return doc



@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}

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

    total = db.attendance_logs.count_documents(q)

    page_docs = list(
        db.attendance_logs.find(q, {"_id": 1, "emp_code": 1, "date": 1,
                                    "status": 1, "punch_in": 1, "punch_out": 1,
                                    "work_hours": 1, "late_minutes": 1,
                                    "overtime_minutes": 1, "half_day": 1,
                                    "history": 1})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for d in page_docs:
        d["id"] = str(d.pop("_id"))
        d["punch_in"] = to_epoch_ms(d.get("punch_in"))
        d["punch_out"] = to_epoch_ms(d.get("punch_out"))

        for entry in d.get("history", []):
            entry["at"] = to_epoch_ms(entry["at"])
            for change in entry.get("changes", {}).values():
                for key in ("from", "to"):
                    if isinstance(change.get(key), datetime):
                        change[key] = to_epoch_ms(change[key])

    return {
        "items": page_docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }



@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    ts = (
        from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else truncate_seconds(datetime.now(timezone.utc))
    )

    # Find the most recent attendance record that began by this time.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    punch_in_time = to_utc(record["punch_in"])
    ts = to_utc(ts)

    elapsed = (ts - punch_in_time).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(
            422, "Punch-out must be after punch-in and within 24 hours"
        )

    work_hours = compute_work_hours(punch_in_time, ts)
    overtime = compute_overtime(
        ts,
        emp["shift_end"],
        record["date"],
        emp["shift_start"],
    )
    half_day = work_hours < 4.50

    # Atomic conditional update: only one concurrent request can win.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
            "punch_in": {"$lte": ts},
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "Already punched out")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated["id"] = str(updated.pop("_id"))
    updated["punch_in"] = to_epoch_ms(updated.get("punch_in"))
    updated["punch_out"] = to_epoch_ms(updated.get("punch_out"))
    updated.setdefault("history", [])
    updated.setdefault("half_day", False)

    return updated



@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    # Validate attendance date.
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )
    if not record:
        raise HTTPException(404, "Attendance record not found")

    supplied = body.model_fields_set
    allowed_statuses = {
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    }

    status = body.status if "status" in supplied else record["status"]

    if status is not None and status not in allowed_statuses:
        raise HTTPException(422, "Invalid attendance status")

    if status in {"ABSENT", "LEAVE"} and (
        "punch_in" in supplied or "punch_out" in supplied
    ):
        raise HTTPException(
            422, "ABSENT and LEAVE cannot include punch times"
        )

    punch_in = (
        from_epoch_ms(body.punch_in)
        if "punch_in" in supplied and body.punch_in is not None
        else (
            None if "punch_in" in supplied
            else record.get("punch_in")
        )
    )
    punch_out = (
        from_epoch_ms(body.punch_out)
        if "punch_out" in supplied and body.punch_out is not None
        else (
            None if "punch_out" in supplied
            else record.get("punch_out")
        )
    )

    punch_in = to_utc(punch_in) if punch_in else None
    punch_out = to_utc(punch_out) if punch_out else None

    if status in {"ABSENT", "LEAVE"}:
        punch_in = punch_out = None

    if status in {"PRESENT", "WFH", "ON_DUTY"} and punch_in is None:
        raise HTTPException(
            422, "Presence status requires a punch-in time"
        )

    # The attendance date is the IST date of punch-in, with
    # overnight shifts assigned to the shift's starting date.
    if punch_in is not None:
        local_in = punch_in.astimezone(IST)
        shift_start = tuple(map(int, emp["shift_start"].split(":")))
        shift_end = tuple(map(int, emp["shift_end"].split(":")))
        expected_date = local_in.date()

        if shift_end <= shift_start and (
            local_in.hour, local_in.minute
        ) < shift_end:
            expected_date -= timedelta(days=1)

        if expected_date.isoformat() != date:
            raise HTTPException(
                422, "punch_in must belong to the attendance date"
            )

    if punch_out is not None:
        if punch_in is None:
            raise HTTPException(422, "punch_out requires punch_in")

        seconds = (punch_out - punch_in).total_seconds()
        if seconds <= 0 or seconds > 24 * 3600:
            raise HTTPException(
                422, "punch_out must be after punch_in and within 24 hours"
            )

    late = (
        compute_late_minutes(
            punch_in, emp["shift_start"], emp["shift_end"]
        )
        if punch_in else 0
    )
    work_hours = (
        compute_work_hours(punch_in, punch_out)
        if punch_in and punch_out else None
    )
    overtime = (
        compute_overtime(
            punch_out, emp["shift_end"], date, emp["shift_start"]
        )
        if punch_in and punch_out else 0
    )
    half_day = work_hours is not None and work_hours < 4.50

    new_values = {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }

    old_values = {
        key: record.get(key)
        for key in new_values
    }

    changes = {}
    for key, new_value in new_values.items():
        old_value = old_values[key]
        if isinstance(old_value, datetime):
            old_value = to_utc(old_value)
        if old_value != new_value:
            if key in {"punch_in", "punch_out"}:
                old_value = to_epoch_ms(old_value)
                new_value_for_history = to_epoch_ms(new_value)
            else:
                new_value_for_history = new_value

            changes[key] = {
                "from": old_value,
                "to": new_value_for_history,
            }

    if not changes:
        raise HTTPException(422, "Request makes no changes")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Compare-and-set prevents concurrent edits from overwriting history.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": record["status"],
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            "history": record.get("history", []),
        },
        {
            "$set": new_values,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record changed; retry")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated["id"] = str(updated.pop("_id"))
    updated["punch_in"] = to_epoch_ms(updated.get("punch_in"))
    updated["punch_out"] = to_epoch_ms(updated.get("punch_out"))

    for entry in updated.get("history", []):
        entry["at"] = to_epoch_ms(entry["at"])

    for entry in updated.get("history", []):
        for change in entry.get("changes", {}).values():
            for key in ("from", "to"):
                if isinstance(change.get(key), datetime):
                    change[key] = to_epoch_ms(change[key])

    return updated

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, mon = map(int, month.split("-"))
    start_date = datetime(year, mon, 1).date()

    if mon == 12:
        end_date = datetime(year + 1, 1, 1).date()
    else:
        end_date = datetime(year, mon + 1, 1).date()

    last_date = end_date - timedelta(days=1)

    # Count working days from the employee's joining date.
    first_work_date = max(start_date, datetime.fromisoformat(emp["joined_on"]).date())
    working_days = 0
    current_date = first_work_date

    while current_date <= last_date:
        if current_date.weekday() < 5:
            working_days += 1
        current_date += timedelta(days=1)

    # Aggregate the employee's attendance records for this month.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": start_date.isoformat(),
                    "$lt": end_date.isoformat()
                }
            }
        },
        {
            "$addFields": {
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                },
                "late_value": {"$ifNull": ["$late_minutes", 0]},
                "overtime_value": {"$ifNull": ["$overtime_minutes", 0]}
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
                                    {"$gte": ["$day_of_week", 2]},
                                    {"$lte": ["$day_of_week", 6]}
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_value", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_value"},
                "total_overtime_minutes": {"$sum": "$overtime_value"}
            }
        }
    ]

    result = list(db.attendance_logs.aggregate(pipeline))

    if result:
        summary = result[0]
    else:
        summary = {
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0
        }

    present_days = summary["present_days"]

    attendance_pct = None
    if working_days > 0:
        attendance_pct = float(
            (Decimal(str(present_days)) * Decimal("100") /
             Decimal(working_days)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": summary["leave_days"],
        "late_count": summary["late_count"],
        "total_late_minutes": summary["total_late_minutes"],
        "total_overtime_minutes": summary["total_overtime_minutes"],
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    year, mon = map(int, month.split("-"))

    start_date = datetime(year, mon, 1).date()

    if mon == 12:
        end_date = datetime(year + 1, 1, 1).date()
    else:
        end_date = datetime(year, mon + 1, 1).date()

    last_date = end_date - timedelta(days=1)
    end_str = end_date.isoformat()
    start_str = start_date.isoformat()

    employee_match = {"joined_on": {"$lte": last_date.isoformat()}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start_str]},
                                    {"$lt": ["$date", end_str]}
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            },
                            "late_value": {"$ifNull": ["$late_minutes", 0]},
                            "overtime_value": {"$ifNull": ["$overtime_minutes", 0]}
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$logs.day_of_week", 2]},
                                    {"$lte": ["$logs.day_of_week", 6]}
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "work_hours_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            None
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_value", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$logs.late_value"},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "avg_work_hours": {
                    "$let": {
                        "vars": {
                            "valid_hours": {
                                "$filter": {
                                    "input": "$work_hours_values",
                                    "as": "hours",
                                    "cond": {"$ne": ["$$hours", None]}
                                }
                            }
                        },
                        "in": {
                            "$cond": [
                                {"$gt": [{"$size": "$$valid_hours"}, 0]},
                                {"$avg": "$$valid_hours"},
                                None
                            ]
                        }
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]

    items = list(db.employees.aggregate(pipeline))

    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = float(
                Decimal(str(item["avg_work_hours"])).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )

    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    year, mon = map(int, month.split("-"))

    start_date = datetime(year, mon, 1).date().isoformat()

    if mon == 12:
        end_date = datetime(year + 1, 1, 1).date().isoformat()
    else:
        end_date = datetime(year, mon + 1, 1).date().isoformat()

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lt": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"}
    ]

    if department is not None:
        pipeline.append({
            "$match": {"employee.department": department}
        })

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1
            }
        }
    ])

    results = list(db.attendance_logs.aggregate(pipeline))

    # Standard competition ranking: 1, 2, 2, 4.
    items = []
    previous_total = None
    current_rank = 0

    for position, employee in enumerate(results, start=1):
        total = employee["total_late_minutes"]

        if total != previous_total:
            current_rank = position
            previous_total = total

        if current_rank > limit:
            break

        employee["rank"] = current_rank

        # Put rank first in the response object.
        items.append({
            "rank": employee["rank"],
            "emp_code": employee["emp_code"],
            "name": employee["name"],
            "department": employee["department"],
            "total_late_minutes": employee["total_late_minutes"],
            "late_count": employee["late_count"]
        })

    return {"month": month, "items": items}

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    try:
        start = datetime.strptime(from_date, "%Y-%m-%d").date()
        end = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD format")

    if end < start:
        raise HTTPException(status_code=422, detail="'to' must not be before 'from'")

    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    employee_filter = {"department": department}
    if not db.employees.find_one(employee_filter):
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": from_date,
                    "$lte": to_date
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {
            "$addFields": {
                "date_value": {
                    "$dateFromString": {"dateString": "$date"}
                },
                "is_present": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                        0
                    ]
                },
                "is_late": {
                    "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                }
            }
        },
        {
            "$group": {
                "_id": "$date_value",
                "present_count": {"$sum": "$is_present"},
                "late_count": {"$sum": "$is_late"}
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(start, datetime.min.time()),
                        datetime.combine(end + timedelta(days=1), datetime.min.time())
                    ]
                }
            }
        },
        {
            "$fill": {
                "output": {
                    "present_count": {"value": 0},
                    "late_count": {"value": 0}
                }
            }
        },
        {
            "$addFields": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$_id",
                        "timezone": "UTC"
                    }
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$_id"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$_id"}, 6]}
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "headcount_data"
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_data.count", 0]},
                        0
                    ]
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]}
                    }
                }
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
                "moving_avg_7d": 1
            }
        },
        {"$sort": {"date": 1}}
    ]

    items = list(db.attendance_logs.aggregate(pipeline))

    for item in items:
        for field in ("attendance_rate", "moving_avg_7d"):
            value = item.get(field)
            if value is not None:
                item[field] = float(
                    Decimal(str(value)).quantize(
                        Decimal("0.0001"),
                        rounding=ROUND_HALF_UP
                    )
                )

    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
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
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in allowed:
        raise HTTPException(422, "Invalid endpoint")

    if endpoint in {
        "employee_monthly", "department_summary", "late_leaderboard"
    } and not month:
        raise HTTPException(422, "month is required")

    if endpoint == "employee_monthly" and not emp_code:
        raise HTTPException(422, "emp_code is required")

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                422, "department, 'from' and 'to' are required"
            )

    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to

        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = "attendance_logs"

    else:
        if endpoint in {
            "employee_monthly", "department_summary", "late_leaderboard"
        }:
            year, mon = map(int, month.split("-"))
            start_date = datetime(year, mon, 1).date()
            end_date = (
                datetime(year + 1, 1, 1).date()
                if mon == 12
                else datetime(year, mon + 1, 1).date()
            )
            start = start_date.isoformat()
            end = end_date.isoformat()

        if endpoint == "employee_monthly":
            pipeline = [
                {"$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start, "$lt": end},
                }},
                {"$addFields": {
                    "day_of_week": {
                        "$dayOfWeek": {
                            "$dateFromString": {"dateString": "$date"}
                        }
                    },
                    "late_value": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_value": {
                        "$ifNull": ["$overtime_minutes", 0]
                    },
                }},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": [
                                "$status", ["PRESENT", "WFH", "ON_DUTY"]
                            ]},
                            {"$gte": ["$day_of_week", 2]},
                            {"$lte": ["$day_of_week", 6]},
                        ]},
                        {"$cond": [
                            {"$eq": ["$half_day", True]}, 0.5, 1
                        ]},
                        0,
                    ]}},
                    "leave_days": {"$sum": {"$cond": [
                        {"$eq": ["$status", "LEAVE"]}, 1, 0
                    ]}},
                    "late_count": {"$sum": {"$cond": [
                        {"$gt": ["$late_value", 0]}, 1, 0
                    ]}},
                    "total_late_minutes": {"$sum": "$late_value"},
                    "total_overtime_minutes": {
                        "$sum": "$overtime_value"
                    },
                }},
            ]
            collection = "attendance_logs"

        elif endpoint == "department_summary":
            employee_match = {
                "joined_on": {"$lte": (end_date - timedelta(days=1)).isoformat()}
            }
            if department is not None:
                employee_match["department"] = department

            pipeline = [
                {"$match": employee_match},
                {"$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {"$match": {"$expr": {"$and": [
                            {"$eq": ["$emp_code", "$$code"]},
                            {"$gte": ["$date", start]},
                            {"$lt": ["$date", end]},
                        ]}}},
                        {"$addFields": {
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            },
                            "late_value": {"$ifNull": ["$late_minutes", 0]},
                            "overtime_value": {
                                "$ifNull": ["$overtime_minutes", 0]
                            },
                        }},
                    ],
                    "as": "logs",
                }},
                {"$unwind": {
                    "path": "$logs",
                    "preserveNullAndEmptyArrays": True,
                }},
                {"$group": {
                    "_id": "$department",
                    "employees": {"$addToSet": "$emp_code"},
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": [
                                "$logs.status", ["PRESENT", "WFH", "ON_DUTY"]
                            ]},
                            {"$gte": ["$logs.day_of_week", 2]},
                            {"$lte": ["$logs.day_of_week", 6]},
                        ]},
                        {"$cond": [
                            {"$eq": ["$logs.half_day", True]}, 0.5, 1
                        ]},
                        0,
                    ]}},
                    "work_hours_values": {"$push": {"$cond": [
                        {"$and": [
                            {"$in": [
                                "$logs.status", ["PRESENT", "WFH", "ON_DUTY"]
                            ]},
                            {"$ne": ["$logs.work_hours", None]},
                        ]},
                        "$logs.work_hours",
                        None,
                    ]}},
                    "late_count": {"$sum": {"$cond": [
                        {"$gt": ["$logs.late_value", 0]}, 1, 0
                    ]}},
                    "total_late_minutes": {"$sum": "$logs.late_value"},
                    "leave_count": {"$sum": {"$cond": [
                        {"$eq": ["$logs.status", "LEAVE"]}, 1, 0
                    ]}},
                    "on_duty_count": {"$sum": {"$cond": [
                        {"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0
                    ]}},
                }},
                {"$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": {"$size": "$employees"},
                    "present_days": 1,
                    "avg_work_hours": {"$let": {
                        "vars": {"valid_hours": {"$filter": {
                            "input": "$work_hours_values",
                            "as": "hours",
                            "cond": {"$ne": ["$$hours", None]},
                        }}},
                        "in": {"$cond": [
                            {"$gt": [{"$size": "$$valid_hours"}, 0]},
                            {"$avg": "$$valid_hours"},
                            None,
                        ]},
                    }},
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }},
                {"$sort": {"department": 1}},
            ]
            collection = "employees"

        elif endpoint == "late_leaderboard":
            pipeline = [
                {"$match": {
                    "date": {"$gte": start, "$lt": end},
                    "late_minutes": {"$gt": 0},
                }},
                {"$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": {"$cond": [
                        {"$gt": ["$late_minutes", 0]}, 1, 0
                    ]}},
                }},
                {"$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }},
                {"$unwind": "$employee"},
            ]
            if department is not None:
                pipeline.append({
                    "$match": {"employee.department": department}
                })

            pipeline.extend([
                {"$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }},
                {"$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1,
                }},
            ])
            collection = "attendance_logs"

        else:
            try:
                start_date = datetime.strptime(
                    from_date, "%Y-%m-%d"
                ).date()
                end_date = datetime.strptime(
                    to_date, "%Y-%m-%d"
                ).date()
            except ValueError:
                raise HTTPException(
                    422, "Dates must use YYYY-MM-DD format"
                )

            if end_date < start_date:
                raise HTTPException(422, "'to' must not be before 'from'")
            if (end_date - start_date).days + 1 > 92:
                raise HTTPException(422, "Date range cannot exceed 92 days")
            if not db.employees.find_one({"department": department}):
                raise HTTPException(404, "Department not found")

            pipeline = [
                {"$match": {
                    "date": {
                        "$gte": from_date,
                        "$lte": to_date,
                    }
                }},
                {"$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }},
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
                {"$addFields": {
                    "date_value": {
                        "$dateFromString": {"dateString": "$date"}
                    },
                    "is_present": {"$cond": [
                        {"$in": [
                            "$status", ["PRESENT", "WFH", "ON_DUTY"]
                        ]},
                        {"$cond": [
                            {"$eq": ["$half_day", True]}, 0.5, 1
                        ]},
                        0,
                    ]},
                    "is_late": {"$cond": [
                        {"$gt": ["$late_minutes", 0]}, 1, 0
                    ]},
                }},
                {"$group": {
                    "_id": "$date_value",
                    "present_count": {"$sum": "$is_present"},
                    "late_count": {"$sum": "$is_late"},
                }},
                {"$densify": {
                    "field": "_id",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [
                            datetime.combine(
                                start_date, datetime.min.time()
                            ),
                            datetime.combine(
                                end_date + timedelta(days=1),
                                datetime.min.time(),
                            ),
                        ],
                    },
                }},
                {"$fill": {"output": {
                    "present_count": {"value": 0},
                    "late_count": {"value": 0},
                }}},
                {"$addFields": {
                    "date": {"$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$_id",
                        "timezone": "UTC",
                    }},
                    "is_working_day": {"$and": [
                        {"$gte": [{"$dayOfWeek": "$_id"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$_id"}, 6]},
                    ]},
                }},
                {"$lookup": {
                    "from": "employees",
                    "let": {"day": "$date"},
                    "pipeline": [
                        {"$match": {"$expr": {"$and": [
                            {"$eq": ["$department", department]},
                            {"$lte": ["$joined_on", "$$day"]},
                        ]}}},
                        {"$count": "count"},
                    ],
                    "as": "headcount_data",
                }},
                {"$addFields": {
                    "headcount": {"$ifNull": [
                        {"$arrayElemAt": ["$headcount_data.count", 0]},
                        0,
                    ]},
                }},
                {"$addFields": {
                    "attendance_rate": {"$cond": [
                        {"$and": [
                            "$is_working_day",
                            {"$gt": ["$headcount", 0]},
                        ]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]},
                }},
                {"$setWindowFields": {
                    "sortBy": {"_id": 1},
                    "output": {"moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }},
                }},
                {"$project": {
                    "_id": 0,
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": 1,
                }},
                {"$sort": {"date": 1}},
            ]
            collection = "attendance_logs"

    try:
        if endpoint == "attendance_list":
            explain = db.command(
                "explain", command, verbosity="executionStats"
            )
        else:
            explain = db.command(
                "explain",
                {
                    "aggregate": collection,
                    "pipeline": pipeline,
                    "cursor": {},
                },
                verbosity="executionStats",
            )
    except PyMongoError as exc:
        raise HTTPException(500, f"Explain failed: {str(exc)}")

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(dumps(explain)),
    }


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out --- DONE
#   PATCH /attendance/{emp_code}/{date} --- DONE
#   GET   /analytics/employees/{emp_code}/monthly --- DONE
#   GET   /analytics/departments/summary --- DONE
#   GET   /analytics/leaderboard/late --- DONE
#   GET   /analytics/departments/{department}/trend --- DONE
#   GET   /admin/explain/{endpoint} --- DONE
# --------------------------------------------------------------------------- #
