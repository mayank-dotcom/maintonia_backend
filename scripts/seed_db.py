import asyncio
import os
import uuid
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from passlib.context import CryptContext
from dotenv import load_dotenv

# Load env from parent dir if needed, or current dir
load_dotenv()

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "employee_management")

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

async def seed():
    print(f"Connecting to MongoDB at {MONGO_URL}...")
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]

    # Clear existing data (optional, but good for a fresh start)
    print("Clearing existing collections...")
    await db.users.delete_many({})
    await db.organizations.delete_many({})
    await db.employees.delete_many({})

    # 1. Create Admin User
    admin_id = str(uuid.uuid4())
    admin_user = {
        "id": admin_id,
        "email": "admin@example.com",
        "name": "Admin User",
        "password_hash": pwd_ctx.hash("password123"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.users.insert_one(admin_user)
    print(f"Created admin user: {admin_user['email']} (password: password123)")

    # 2. Create Organization
    org_id = str(uuid.uuid4())
    org = {
        "id": org_id,
        "owner_id": admin_id,
        "name": "Tech Corp",
        "industry": "Technology",
        "description": "A sample tech company for testing.",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "deleted": False,
    }
    await db.organizations.insert_one(org)
    print(f"Created organization: {org['name']}")

    # 3. Create Employees
    employees = [
        {
            "id": str(uuid.uuid4()),
            "owner_id": admin_id,
            "full_name": "John Doe",
            "email": "john@techcorp.com",
            "phone": "+1234567890",
            "position": "Software Engineer",
            "department": "Engineering",
            "organization_id": org_id,
            "organization_name": org["name"],
            "joining_date": "2023-01-15T09:00:00Z",
            "salary": 85000.0,
            "status": "Active",
            "deleted": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        {
            "id": str(uuid.uuid4()),
            "owner_id": admin_id,
            "full_name": "Jane Smith",
            "email": "jane@techcorp.com",
            "phone": "+1987654321",
            "position": "Product Manager",
            "department": "Product",
            "organization_id": org_id,
            "organization_name": org["name"],
            "joining_date": "2023-03-10T10:00:00Z",
            "salary": 95000.0,
            "status": "Active",
            "deleted": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        {
            "id": str(uuid.uuid4()),
            "owner_id": admin_id,
            "full_name": "Alice Johnson",
            "email": "alice@techcorp.com",
            "phone": "+1122334455",
            "position": "Designer",
            "department": "Design",
            "organization_id": org_id,
            "organization_name": org["name"],
            "joining_date": "2023-05-20T08:30:00Z",
            "salary": 75000.0,
            "status": "Inactive",
            "deleted": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ]
    await db.employees.insert_many(employees)
    print(f"Inserted {len(employees)} sample employees.")

    print("Seeding completed successfully!")
    client.close()

if __name__ == "__main__":
    asyncio.run(seed())
