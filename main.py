"""FastAPI backend for Employee Management System."""
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field, field_validator
from passlib.context import CryptContext
from jose import jwt, JWTError

load_dotenv()

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "employee_management")

JWT_SECRET = os.getenv("JWT_SECRET", "super-secret-jwt-key-change-me-in-prod-9f8e7d6c5b4a")
JWT_ALGO = "HS256"
JWT_EXP_MIN = 60 * 24 * 7  # 7 days

app = FastAPI(title="Employee Management API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add serverSelectionTimeoutMS to prevent hanging indefinitely if MongoDB is not running
client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=5000)
db = client[DB_NAME]

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

PHONE_RE = re.compile(r"^[+]?[\d\s\-()]{7,20}$")
NAME_RE = re.compile(r"^[A-Za-z\s\.'-]{2,}$")


# ---------- Models ----------
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    name: str = Field(min_length=2)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    industry: Optional[str] = None
    description: Optional[str] = None


class EmployeeIn(BaseModel):
    full_name: str
    email: EmailStr
    phone: str
    position: str
    department: str
    organization_id: str
    joining_date: str  # ISO date string
    salary: Optional[float] = None
    status: str = "Active"

    @field_validator("full_name")
    @classmethod
    def _name(cls, v):
        v = v.strip()
        if not NAME_RE.match(v):
            raise ValueError("Name must be at least 2 chars, letters/spaces only")
        return v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        v = v.strip()
        if not PHONE_RE.match(v):
            raise ValueError("Invalid phone number format")
        return v

    @field_validator("salary")
    @classmethod
    def _salary(cls, v):
        if v is not None and v < 0:
            raise ValueError("Salary must be positive")
        return v

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v not in ("Active", "Inactive"):
            raise ValueError("Status must be Active or Inactive")
        return v


class EmployeeUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    position: Optional[str] = None
    department: Optional[str] = None
    organization_id: Optional[str] = None
    joining_date: Optional[str] = None
    salary: Optional[float] = None
    status: Optional[str] = None


# ---------- Helpers ----------
def _clean(doc):
    if not doc:
        return doc
    doc.pop("_id", None)
    return doc


def _make_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXP_MIN),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


async def get_current_user(token: Optional[str] = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        uid = payload.get("sub")
    except JWTError:
        raise HTTPException(401, "Invalid token")
    user = await db.users.find_one({"id": uid})
    if not user:
        raise HTTPException(401, "User not found")
    return _clean(user)


# ---------- Routes ----------
@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "fastapi", "db": DB_NAME}


# Auth
@app.post("/api/auth/register")
async def register(body: RegisterIn):
    existing = await db.users.find_one({"email": body.email.lower()})
    if existing:
        raise HTTPException(400, "Email already registered")
    uid = str(uuid.uuid4())
    user = {
        "id": uid,
        "email": body.email.lower(),
        "name": body.name.strip(),
        "password_hash": pwd_ctx.hash(body.password),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.users.insert_one(user)
    token = _make_token(uid)
    return {"token": token, "user": {"id": uid, "email": user["email"], "name": user["name"]}}


@app.post("/api/auth/login")
async def login(body: LoginIn):
    user = await db.users.find_one({"email": body.email.lower()})
    if not user or not pwd_ctx.verify(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    token = _make_token(user["id"])
    return {"token": token, "user": {"id": user["id"], "email": user["email"], "name": user["name"]}}


@app.get("/api/auth/me")
async def me(user=Depends(get_current_user)):
    return {"id": user["id"], "email": user["email"], "name": user["name"]}


# Organizations
@app.get("/api/organizations")
async def list_orgs(user=Depends(get_current_user)):
    docs = await db.organizations.find({"owner_id": user["id"], "deleted": {"$ne": True}}).sort("created_at", -1).to_list(500)
    return [_clean(d) for d in docs]


@app.post("/api/organizations")
async def create_org(body: OrgIn, user=Depends(get_current_user)):
    oid = str(uuid.uuid4())
    doc = {
        "id": oid,
        "owner_id": user["id"],
        "name": body.name.strip(),
        "industry": (body.industry or "").strip() or None,
        "description": (body.description or "").strip() or None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "deleted": False,
    }
    await db.organizations.insert_one(doc)
    return _clean(doc)


@app.delete("/api/organizations/{org_id}")
async def delete_org(org_id: str, user=Depends(get_current_user)):
    res = await db.organizations.update_one(
        {"id": org_id, "owner_id": user["id"]},
        {"$set": {"deleted": True}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Organization not found")
    return {"ok": True}


# Employees
@app.get("/api/employees")
async def list_employees(
    user=Depends(get_current_user),
    query: Optional[str] = None,
    org: Optional[str] = None,
    dept: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    include_deleted: bool = False,
    page: int = 1,
    page_size: int = 10,
):
    q = {"owner_id": user["id"]}
    if not include_deleted:
        q["deleted"] = {"$ne": True}
    else:
        q["deleted"] = True
    if org:
        q["organization_id"] = org
    if dept:
        q["department"] = dept
    if status_filter:
        q["status"] = status_filter
    if query:
        rx = {"$regex": re.escape(query), "$options": "i"}
        q["$or"] = [
            {"full_name": rx},
            {"email": rx},
            {"position": rx},
            {"department": rx},
        ]
    total = await db.employees.count_documents(q)
    skip = max(0, (page - 1) * page_size)
    docs = await db.employees.find(q).sort("created_at", -1).skip(skip).limit(page_size).to_list(page_size)
    return {
        "items": [_clean(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if page_size else 1,
    }


@app.post("/api/employees")
async def create_employee(body: EmployeeIn, user=Depends(get_current_user)):
    email = body.email.lower()
    # uniqueness (not deleted)
    existing = await db.employees.find_one({"owner_id": user["id"], "email": email, "deleted": {"$ne": True}})
    if existing:
        raise HTTPException(400, "Email already exists for another employee")
    # org exists
    org = await db.organizations.find_one({"id": body.organization_id, "owner_id": user["id"], "deleted": {"$ne": True}})
    if not org:
        raise HTTPException(400, "Invalid organization")
    eid = str(uuid.uuid4())
    doc = {
        "id": eid,
        "owner_id": user["id"],
        "full_name": body.full_name.strip(),
        "email": email,
        "phone": body.phone.strip(),
        "position": body.position.strip(),
        "department": body.department.strip(),
        "organization_id": body.organization_id,
        "organization_name": org["name"],
        "joining_date": body.joining_date,
        "salary": body.salary,
        "status": body.status,
        "deleted": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.employees.insert_one(doc)
    return _clean(doc)


@app.put("/api/employees/{emp_id}")
async def update_employee(emp_id: str, body: EmployeeUpdate, user=Depends(get_current_user)):
    emp = await db.employees.find_one({"id": emp_id, "owner_id": user["id"], "deleted": {"$ne": True}})
    if not emp:
        raise HTTPException(404, "Employee not found")
    update = {}
    data = body.model_dump(exclude_none=True)
    if "email" in data:
        email = data["email"].lower()
        dup = await db.employees.find_one({"owner_id": user["id"], "email": email, "id": {"$ne": emp_id}, "deleted": {"$ne": True}})
        if dup:
            raise HTTPException(400, "Email already exists for another employee")
        update["email"] = email
    if "full_name" in data:
        v = data["full_name"].strip()
        if not NAME_RE.match(v):
            raise HTTPException(400, "Invalid name")
        update["full_name"] = v
    if "phone" in data:
        v = data["phone"].strip()
        if not PHONE_RE.match(v):
            raise HTTPException(400, "Invalid phone")
        update["phone"] = v
    if "salary" in data:
        if data["salary"] is not None and data["salary"] < 0:
            raise HTTPException(400, "Salary must be positive")
        update["salary"] = data["salary"]
    if "status" in data:
        if data["status"] not in ("Active", "Inactive"):
            raise HTTPException(400, "Invalid status")
        update["status"] = data["status"]
    for k in ("position", "department", "joining_date"):
        if k in data:
            update[k] = data[k]
    if "organization_id" in data:
        org = await db.organizations.find_one({"id": data["organization_id"], "owner_id": user["id"], "deleted": {"$ne": True}})
        if not org:
            raise HTTPException(400, "Invalid organization")
        update["organization_id"] = data["organization_id"]
        update["organization_name"] = org["name"]
    update["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db.employees.update_one({"id": emp_id}, {"$set": update})
    new_doc = await db.employees.find_one({"id": emp_id})
    return _clean(new_doc)


@app.delete("/api/employees/{emp_id}")
async def soft_delete_employee(emp_id: str, user=Depends(get_current_user)):
    res = await db.employees.update_one(
        {"id": emp_id, "owner_id": user["id"], "deleted": {"$ne": True}},
        {"$set": {"deleted": True, "deleted_at": datetime.now(timezone.utc).isoformat()}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Employee not found")
    return {"ok": True}


@app.post("/api/employees/{emp_id}/restore")
async def restore_employee(emp_id: str, user=Depends(get_current_user)):
    res = await db.employees.update_one(
        {"id": emp_id, "owner_id": user["id"], "deleted": True},
        {"$set": {"deleted": False}, "$unset": {"deleted_at": ""}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Employee not found")
    return {"ok": True}


@app.get("/api/stats")
async def stats(user=Depends(get_current_user)):
    base = {"owner_id": user["id"], "deleted": {"$ne": True}}
    total = await db.employees.count_documents(base)
    active = await db.employees.count_documents({**base, "status": "Active"})
    inactive = await db.employees.count_documents({**base, "status": "Inactive"})
    orgs_count = await db.organizations.count_documents({"owner_id": user["id"], "deleted": {"$ne": True}})

    # per-org
    per_org_cursor = db.employees.aggregate([
        {"$match": base},
        {"$group": {"_id": {"org": "$organization_name"}, "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ])
    per_org = [{"name": d["_id"]["org"] or "Unknown", "value": d["count"]} async for d in per_org_cursor]

    per_dept_cursor = db.employees.aggregate([
        {"$match": base},
        {"$group": {"_id": "$department", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ])
    per_dept = [{"name": d["_id"] or "Unknown", "value": d["count"]} async for d in per_dept_cursor]

    return {
        "total": total,
        "active": active,
        "inactive": inactive,
        "organizations": orgs_count,
        "by_organization": per_org,
        "by_department": per_dept,
    }


@app.on_event("startup")
async def startup():
    # Indexes
    await db.users.create_index("email", unique=True)
    await db.employees.create_index([("owner_id", 1), ("email", 1)])
    await db.employees.create_index([("owner_id", 1), ("deleted", 1)])
    await db.organizations.create_index([("owner_id", 1), ("deleted", 1)])
