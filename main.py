"""FastAPI backend for Employee Management System."""
import os
import re
import uuid
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field, field_validator
from passlib.context import CryptContext
from jose import jwt, JWTError
from groq import Groq

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

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Store chat history per user (in-memory, resets on restart)
chat_history = {}
chat_state = {}
MAX_HISTORY = 20  # Keep last 20 messages per user

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


class ChatIn(BaseModel):
    message: str


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


def _append_chat(user_id: str, role: str, content: str) -> None:
    if user_id not in chat_history:
        chat_history[user_id] = []
    chat_history[user_id].append({"role": role, "content": content})
    chat_history[user_id] = chat_history[user_id][-MAX_HISTORY:]


def _format_employee_details(emp: dict) -> str:
    salary = emp.get("salary")
    salary_text = f"${salary:,.2f}" if isinstance(salary, (int, float)) else "N/A"
    return (
        "Employee Details:\n"
        f"- Name: {emp.get('full_name', 'N/A')}\n"
        f"- Email: {emp.get('email', 'N/A')}\n"
        f"- Phone: {emp.get('phone', 'N/A')}\n"
        f"- Position: {emp.get('position', 'N/A')}\n"
        f"- Department: {emp.get('department', 'N/A')}\n"
        f"- Organization: {emp.get('organization_name', 'N/A')}\n"
        f"- Joining Date: {emp.get('joining_date', 'N/A')}\n"
        f"- Salary: {salary_text}\n"
        f"- Status: {emp.get('status', 'N/A')}"
    )


def _is_general_message(text_lower: str) -> bool:
    greetings = (
        "hi", "hello", "hey", "namaste", "good morning", "good evening",
        "thank", "thanks", "bye", "how are you", "kaise ho", "kya haal"
    )
    data_words = (
        "employee", "org", "organization", "count", "total", "number", "kitne",
        "salary", "department", "position", "phone", "email", "stats", "summary",
        "detail", "active", "inactive", "list", "show"
    )
    return any(k in text_lower for k in greetings) and not any(k in text_lower for k in data_words)


def _asked_for_full_details(text_lower: str) -> bool:
    markers = ("all details", "full details", "complete details", "saari detail", "sab detail", "about")
    return any(m in text_lower for m in markers)


def _extract_email(text: str) -> Optional[str]:
    match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    return match.group(0).lower() if match else None


def _extract_phone_fragment(text: str) -> Optional[str]:
    digits = re.sub(r"\D", "", text)
    return digits[-10:] if len(digits) >= 7 else None


def _parse_joining_date(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if len(text) <= 10:
                dt = datetime.fromisoformat(text)
            else:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def _contains_any(text: str, options: List[str]) -> bool:
    return any(opt in text for opt in options)


def _best_org_match(orgs: List[dict], user_lower: str, hint: Optional[str] = None) -> Optional[dict]:
    candidates = [o for o in orgs if (o.get("name") or "").strip()]
    if not candidates:
        return None
    if hint:
        h = hint.strip().lower()
        for o in candidates:
            name = o["name"].strip().lower()
            if h == name or h in name or name in h:
                return o
    for o in candidates:
        name = o["name"].strip().lower()
        if name and name in user_lower:
            return o
    return None


def _llm_intent(
    user_message: str,
    context_messages: List[dict],
    org_names: List[str],
    state: dict,
) -> Dict[str, Any]:
    default_intent = {
        "topic": "unknown",
        "action": "unknown",
        "target": "unknown",
        "use_previous_org": False,
        "use_previous_employee": False,
        "mentioned_org": "",
        "asked_fields": [],
    }
    if not groq_client:
        return default_intent

    context_text = "\n".join([f"{m.get('role', 'user')}: {m.get('content', '')}" for m in context_messages[-8:]])
    org_text = ", ".join(org_names[:100]) or "none"
    prompt = f"""
You are an intent parser for an HR assistant. Return STRICT JSON only.
No markdown, no explanation.

User message: "{user_message}"
Conversation context:
{context_text}

Known organizations: {org_text}
Current state: last_topic={state.get("last_topic")}, has_last_org={bool(state.get("last_org_id"))}, has_last_employee={bool(state.get("last_employee_id"))}

Output JSON schema:
{{
  "topic": "conversation|employee|organization|stats|unknown",
  "action": "count|list|details|name|summary|conversation|unknown",
  "target": "employee|organization|both|unknown",
  "use_previous_org": true/false,
  "use_previous_employee": true/false,
  "mentioned_org": "organization name if explicit else empty",
  "asked_fields": ["name","email","phone","position","department","organization","joining","salary","status"]
}}
"""
    try:
        resp = groq_client.chat.completions.create(
            model="qwen/qwen3-32b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=220,
        )
        text = (resp.choices[0].message.content or "").replace("<think>", "").replace("</think>", "").strip()
        parsed = _extract_json_object(text)
        if not parsed:
            return default_intent
        out = default_intent.copy()
        out.update({k: parsed.get(k, out[k]) for k in out.keys()})
        if not isinstance(out.get("asked_fields"), list):
            out["asked_fields"] = []
        return out
    except Exception:
        return default_intent


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


@app.post("/api/chatbot")
async def chatbot_query(body: ChatIn, user=Depends(get_current_user)):
    user_message = (body.message or "").strip()
    if not user_message:
        raise HTTPException(400, "Message cannot be empty")

    user_id = user["id"]
    user_lower = user_message.lower()
    base_filter = {"owner_id": user_id, "deleted": {"$ne": True}}

    if user_id not in chat_state:
        chat_state[user_id] = {"last_employee_id": None, "last_org_id": None, "last_topic": None}
    state = chat_state[user_id]

    _append_chat(user_id, "user", user_message)

    try:
        if _is_general_message(user_lower):
            response = "Sure, I am here to help. You can ask general questions and employee or organization queries."
            if groq_client:
                prompt = (
                    "You are a friendly HR assistant. Reply naturally in ENGLISH only in 1-2 short lines. "
                    "No hidden reasoning. User message: "
                    f"{user_message}"
                )
                chat = groq_client.chat.completions.create(
                    model="qwen/qwen3-32b",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.5,
                    max_tokens=120,
                )
                response = chat.choices[0].message.content.strip().replace("<think>", "").replace("</think>", "").strip()
            _append_chat(user_id, "assistant", response)
            return {"response": response, "query_type": "conversation"}

        orgs = await db.organizations.find({"owner_id": user_id, "deleted": {"$ne": True}}).to_list(300)
        org_names = [(o.get("name") or "").strip() for o in orgs if (o.get("name") or "").strip()]
        intent = _llm_intent(user_message, chat_history.get(user_id, []), org_names, state)
        mentioned_org = _best_org_match(orgs, user_lower, intent.get("mentioned_org"))

        email_in_query = _extract_email(user_message)
        phone_fragment = _extract_phone_fragment(user_message)

        refers_previous = _contains_any(
            user_lower,
            [
                "his",
                "her",
                "that employee",
                "same employee",
                "previous",
                "uska",
                "ussi",
                "uski",
                "its",
                "that org",
                "that organization",
                "that one",
                "there",
                "that place",
                "those",
                "them",
                "it",
            ],
        ) or bool(intent.get("use_previous_org")) or bool(intent.get("use_previous_employee"))
        if not mentioned_org and refers_previous and state.get("last_org_id"):
            mentioned_org = await db.organizations.find_one({"owner_id": user_id, "deleted": {"$ne": True}, "id": state["last_org_id"]})

        asks_count = _contains_any(user_lower, ["count", "total", "number", "kitne", "kitni", "kitte", "kitti", "how many", "many"]) or intent.get("action") == "count"
        asks_list = _contains_any(user_lower, ["list", "show", "display", "bata", "dikha", "which", "who all", "all", "kaun", "kon", "koun"]) or intent.get("action") == "list"
        asks_org = _contains_any(user_lower, ["organization", "organizations", "organisation", "organisations", "org", "company", "companies"]) or intent.get("target") in ("organization", "both") or intent.get("topic") == "organization"
        asks_employee = _contains_any(user_lower, ["employee", "employees", "staff", "people", "member", "members", "workforce", "works there", "who works", "work in", "working in", "kaun"]) or intent.get("target") in ("employee", "both") or intent.get("topic") == "employee"
        if asks_count and not asks_org and not asks_employee and state.get("last_topic") in ("employee", "organization"):
            asks_employee = state["last_topic"] == "employee"
            asks_org = state["last_topic"] == "organization"
        asks_stats = _contains_any(user_lower, ["stats", "statistics", "summary", "overview", "dashboard"]) or intent.get("action") == "summary" or intent.get("topic") == "stats"
        asks_dept = "department" in user_lower
        asks_salary = "salary" in user_lower
        asks_average = _contains_any(user_lower, ["average", "avg", "mean"])
        asks_joining_rate = _contains_any(
            user_lower,
            [
                "joining rate",
                "join rate",
                "hiring rate",
                "joining trend",
                "joining stats",
                "new joiners",
                "joiners",
            ],
        )
        if "joining" in user_lower and "rate" in user_lower:
            asks_joining_rate = True
        asks_name = any(k in user_lower for k in ("name", "naam"))
        asks_org_name = asks_name and (asks_org or ("organization" in (state.get("last_topic") or "") and refers_previous))
        asks_people_in_org = _contains_any(
            user_lower,
            [
                "who works",
                "who work",
                "works there",
                "work there",
                "who is working",
                "who are working",
                "people in",
                "members in",
                "employees there",
                "team in",
                "who in",
                "kaun hai",
                "mei kaun",
                "mein kaun",
                "work in",
                "working in",
            ],
        ) or (asks_employee and asks_list and (mentioned_org is not None or refers_previous))

        analytics_org = mentioned_org
        if not analytics_org and refers_previous and state.get("last_org_id"):
            analytics_org = await db.organizations.find_one(
                {"owner_id": user_id, "deleted": {"$ne": True}, "id": state["last_org_id"]}
            )

        if asks_salary and asks_average and analytics_org:
            salary_pipeline = [
                {"$match": {**base_filter, "organization_id": analytics_org["id"], "salary": {"$ne": None}}},
                {"$group": {"_id": None, "avg": {"$avg": "$salary"}, "count": {"$sum": 1}}},
            ]
            salary_docs = [d async for d in db.employees.aggregate(salary_pipeline)]
            avg_salary = salary_docs[0]["avg"] if salary_docs else 0
            sample_size = salary_docs[0]["count"] if salary_docs else 0
            response = (
                f"The average salary in {analytics_org['name']} is ${avg_salary:,.2f} "
                f"(based on {sample_size} employees with salary data)."
            )
            state["last_topic"] = "organization"
            state["last_org_id"] = analytics_org["id"]
            _append_chat(user_id, "assistant", response)
            return {"response": response, "query_type": "organization_analytics"}

        if asks_joining_rate and analytics_org:
            window_days = 30
            if _contains_any(user_lower, ["quarter", "3 month", "three month", "90 day"]):
                window_days = 90
            elif _contains_any(user_lower, ["year", "12 month", "365 day"]):
                window_days = 365

            org_emps = await db.employees.find({**base_filter, "organization_id": analytics_org["id"]}).to_list(1000)
            total_org_emp = len(org_emps)
            cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
            recent_joiners = 0
            for emp_doc in org_emps:
                jd = _parse_joining_date(emp_doc.get("joining_date"))
                if jd and jd >= cutoff:
                    recent_joiners += 1
            rate = (recent_joiners / total_org_emp * 100) if total_org_emp else 0.0
            response = (
                f"Joining rate in {analytics_org['name']} for last {window_days} days is {rate:.2f}% "
                f"({recent_joiners}/{total_org_emp} employees joined in this period)."
            )
            state["last_topic"] = "organization"
            state["last_org_id"] = analytics_org["id"]
            _append_chat(user_id, "assistant", response)
            return {"response": response, "query_type": "organization_analytics"}

        if asks_org and asks_name and (asks_list or "all" in user_lower or "organizations" in user_lower):
            if orgs:
                org_names = "\n".join([f"- {o.get('name', 'N/A')}" for o in orgs])
                response = f"Registered organizations:\n{org_names}"
                state["last_topic"] = "organization"
                if len(orgs) == 1:
                    state["last_org_id"] = orgs[0]["id"]
            else:
                response = "No organization is registered yet."
            _append_chat(user_id, "assistant", response)
            return {"response": response, "query_type": "data"}

        if mentioned_org and (asks_people_in_org or asks_count or (asks_employee and asks_list) or (asks_employee and asks_count)):
            org_filter = {**base_filter, "organization_id": mentioned_org["id"]}
            if asks_count and not asks_list:
                cnt = await db.employees.count_documents(org_filter)
                response = f"There are {cnt} employees in {mentioned_org['name']}."
            else:
                docs = await db.employees.find(org_filter).sort("full_name", 1).limit(25).to_list(25)
                if docs:
                    lines = [f"- {d.get('full_name', 'N/A')} ({d.get('position', 'N/A')}, {d.get('department', 'N/A')})" for d in docs]
                    response = f"Employees in {mentioned_org['name']}:\n" + "\n".join(lines)
                else:
                    response = f"No employees found in {mentioned_org['name']}."
            state["last_org_id"] = mentioned_org["id"]
            state["last_topic"] = "organization"
            _append_chat(user_id, "assistant", response)
            return {"response": response, "query_type": "organization_employees"}

        if (asks_people_in_org or asks_count) and (mentioned_org or ("organization" in (state.get("last_topic") or "") and refers_previous)):
            org_for_people = mentioned_org
            if not org_for_people and state.get("last_org_id"):
                org_for_people = await db.organizations.find_one(
                    {"owner_id": user_id, "deleted": {"$ne": True}, "id": state["last_org_id"]}
                )
            if org_for_people:
                org_scope_filter = {**base_filter, "organization_id": org_for_people["id"]}
                if asks_count and not asks_list:
                    cnt = await db.employees.count_documents(org_scope_filter)
                    response = f"There are {cnt} employees in {org_for_people['name']}."
                else:
                    docs = await db.employees.find(org_scope_filter).sort("full_name", 1).limit(25).to_list(25)
                    if docs:
                        lines = [f"- {d.get('full_name', 'N/A')} ({d.get('position', 'N/A')})" for d in docs]
                        response = f"People working in {org_for_people['name']}:\n" + "\n".join(lines)
                    else:
                        response = f"No employees are currently mapped to {org_for_people['name']}."
                state["last_topic"] = "organization"
                state["last_org_id"] = org_for_people["id"]
                _append_chat(user_id, "assistant", response)
                return {"response": response, "query_type": "organization_employees"}

        if asks_org and asks_count and not asks_employee:
            org_count = await db.organizations.count_documents({"owner_id": user_id, "deleted": {"$ne": True}})
            response = f"You have {org_count} registered organizations."
            if org_count == 1 and orgs:
                state["last_org_id"] = orgs[0]["id"]
            state["last_topic"] = "organization"
            _append_chat(user_id, "assistant", response)
            return {"response": response, "query_type": "data"}

        if asks_org_name:
            org_for_name = mentioned_org
            if not org_for_name and state.get("last_org_id"):
                org_for_name = await db.organizations.find_one({"owner_id": user_id, "deleted": {"$ne": True}, "id": state["last_org_id"]})
            if not org_for_name and len(orgs) == 1:
                org_for_name = orgs[0]
            if org_for_name:
                state["last_org_id"] = org_for_name["id"]
                state["last_topic"] = "organization"
                response = f"The organization name is {org_for_name.get('name', 'N/A')}."
            elif orgs:
                response = "I found multiple organizations. Please tell me which one you want the name for."
            else:
                response = "No organization is registered yet."
            _append_chat(user_id, "assistant", response)
            return {"response": response, "query_type": "data"}

        if asks_org and (asks_list or "wise" in user_lower or "through" in user_lower or "per org" in user_lower):
            pipeline = [
                {"$match": base_filter},
                {"$group": {"_id": "$organization_name", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
            ]
            result = [{"org": r["_id"] or "Unknown", "count": r["count"]} async for r in db.employees.aggregate(pipeline)]
            if result:
                response = "Organization-wise employee count:\n" + "\n".join([f"- {r['org']}: {r['count']}" for r in result])
            else:
                response = "No organization-wise data is available yet."
            state["last_topic"] = "organization"
            _append_chat(user_id, "assistant", response)
            return {"response": response, "query_type": "data"}

        if asks_dept and (asks_count or asks_list or asks_stats):
            pipeline = [
                {"$match": base_filter},
                {"$group": {"_id": "$department", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
            ]
            result = [{"department": r["_id"] or "Unknown", "count": r["count"]} async for r in db.employees.aggregate(pipeline)]
            if result:
                response = "Department-wise employee count:\n" + "\n".join([f"- {r['department']}: {r['count']}" for r in result])
            else:
                response = "No department data is available yet."
            _append_chat(user_id, "assistant", response)
            return {"response": response, "query_type": "data"}

        if asks_salary and any(k in user_lower for k in ("average", "avg", "mean")):
            salary_pipeline = [
                {"$match": {**base_filter, "salary": {"$ne": None}}},
                {"$group": {"_id": None, "avg": {"$avg": "$salary"}}},
            ]
            salary_docs = [d async for d in db.employees.aggregate(salary_pipeline)]
            avg_salary = salary_docs[0]["avg"] if salary_docs else 0
            response = f"The average salary is ${avg_salary:,.2f}."
            _append_chat(user_id, "assistant", response)
            return {"response": response, "query_type": "data"}

        if asks_stats:
            total = await db.employees.count_documents(base_filter)
            active = await db.employees.count_documents({**base_filter, "status": "Active"})
            inactive = await db.employees.count_documents({**base_filter, "status": "Inactive"})
            org_count = await db.organizations.count_documents({"owner_id": user_id, "deleted": {"$ne": True}})
            response = (
                "Quick Summary:\n"
                f"- Total Employees: {total}\n"
                f"- Active: {active}\n"
                f"- Inactive: {inactive}\n"
                f"- Total Organizations: {org_count}"
            )
            state["last_topic"] = "organization"
            _append_chat(user_id, "assistant", response)
            return {"response": response, "query_type": "data"}

        if asks_employee and asks_count:
            total = await db.employees.count_documents(base_filter)
            state["last_topic"] = "employee"
            response = f"There are {total} employees in total."
            _append_chat(user_id, "assistant", response)
            return {"response": response, "query_type": "data"}

        employee = None
        explicit_employee_detail = _contains_any(
            user_lower,
            [
                "employee detail",
                "employee info",
                "about employee",
                "whose",
                "who is",
                "who he is",
                "who she is",
                "who is he",
                "who is she",
                "uska",
                "his",
                "her",
                "email",
                "phone",
                "position",
                "joining",
                "status",
            ],
        ) or intent.get("action") == "details" or bool(intent.get("asked_fields"))

        if (explicit_employee_detail or email_in_query or phone_fragment) and state.get("last_employee_id") and refers_previous:
            employee = await db.employees.find_one({**base_filter, "id": state["last_employee_id"]})

        if not employee and email_in_query:
            employee = await db.employees.find_one({**base_filter, "email": email_in_query})

        if not employee and phone_fragment:
            employee = await db.employees.find_one({**base_filter, "phone": {"$regex": re.escape(phone_fragment), "$options": "i"}})

        if not employee and explicit_employee_detail:
            tokens = [
                t for t in re.findall(r"[A-Za-z0-9@._+-]+", user_message)
                if len(t) >= 3 and t.lower() not in {"show", "list", "employee", "employees", "details", "detail", "about", "please", "org", "organization", "name"}
            ][:6]
            or_filters = []
            for tok in tokens:
                rx = {"$regex": re.escape(tok), "$options": "i"}
                for field in ("full_name", "email", "position", "department", "organization_name"):
                    or_filters.append({field: rx})
            if or_filters:
                employee = await db.employees.find_one({**base_filter, "$or": or_filters})

        # Context bridge: if previous topic is an organization, resolve pronoun-based employee follow-ups.
        if not employee and explicit_employee_detail and state.get("last_org_id"):
            org_emp_filter = {**base_filter, "organization_id": state["last_org_id"]}
            org_emp_count = await db.employees.count_documents(org_emp_filter)
            if org_emp_count == 1:
                employee = await db.employees.find_one(org_emp_filter)
            elif org_emp_count > 1 and _contains_any(user_lower, ["who", "which one", "kaun"]):
                docs = await db.employees.find(org_emp_filter).sort("full_name", 1).limit(10).to_list(10)
                lines = [f"- {d.get('full_name', 'N/A')} ({d.get('position', 'N/A')})" for d in docs]
                response = "There are multiple employees in that organization. Please choose one:\n" + "\n".join(lines)
                _append_chat(user_id, "assistant", response)
                return {"response": response, "query_type": "organization_employees"}

        wants_specific_employee = employee is not None or explicit_employee_detail
        if wants_specific_employee:
            if not employee:
                response = "I could not identify the employee. Please share a name, email, phone, department, or organization hint."
                _append_chat(user_id, "assistant", response)
                return {"response": response, "query_type": "employee_details"}

            employee = _clean(employee)
            state["last_employee_id"] = employee["id"]
            state["last_topic"] = "employee"
            if employee.get("organization_id"):
                state["last_org_id"] = employee["organization_id"]

            field_map = {
                "name": ("Name", employee.get("full_name", "N/A")),
                "email": ("Email", employee.get("email", "N/A")),
                "phone": ("Phone", employee.get("phone", "N/A")),
                "position": ("Position", employee.get("position", "N/A")),
                "department": ("Department", employee.get("department", "N/A")),
                "organization": ("Organization", employee.get("organization_name", "N/A")),
                "joining": ("Joining Date", employee.get("joining_date", "N/A")),
                "salary": ("Salary", f"{employee.get('salary', 'N/A')}"),
                "status": ("Status", employee.get("status", "N/A")),
            }

            asked_fields = [k for k in field_map.keys() if k in user_lower]
            llm_fields = [f for f in intent.get("asked_fields", []) if f in field_map]
            asked_fields = list(dict.fromkeys(asked_fields + llm_fields))
            if asked_fields and not _asked_for_full_details(user_lower):
                lines = [f"- {field_map[k][0]}: {field_map[k][1]}" for k in asked_fields]
                response = f"Requested details for {employee.get('full_name', 'Employee')}:\n" + "\n".join(lines)
            else:
                response = _format_employee_details(employee)

            _append_chat(user_id, "assistant", response)
            return {"response": response, "query_type": "employee_details"}

        if asks_employee and asks_list:
            docs = await db.employees.find(base_filter).sort("created_at", -1).limit(15).to_list(15)
            if docs:
                lines = [f"- {d.get('full_name', 'N/A')} ({d.get('position', 'N/A')}, {d.get('organization_name', 'N/A')})" for d in docs]
                response = f"Latest {len(docs)} employees:\n" + "\n".join(lines)
            else:
                response = "No employee data is available yet."
            state["last_topic"] = "employee"
            _append_chat(user_id, "assistant", response)
            return {"response": response, "query_type": "data"}

        fallback_total = await db.employees.count_documents(base_filter)
        fallback_orgs = await db.organizations.count_documents({"owner_id": user_id, "deleted": {"$ne": True}})
        response = (
            f"I can help with this. Right now you have {fallback_total} employees and {fallback_orgs} organizations. "
            "You can ask for employee details, organization-wise employees, department counts, salary average, or summary stats."
        )
        _append_chat(user_id, "assistant", response)
        return {"response": response, "query_type": "fallback"}
    except Exception as e:
        print(f"Chatbot Error: {e}")
        raise HTTPException(500, f"Chatbot error: {str(e)}")


@app.on_event("startup")
async def startup():
    # Indexes
    await db.users.create_index("email", unique=True)
    await db.employees.create_index([("owner_id", 1), ("email", 1)])
    await db.employees.create_index([("owner_id", 1), ("deleted", 1)])
    await db.organizations.create_index([("owner_id", 1), ("deleted", 1)])
