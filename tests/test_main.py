import pytest
from datetime import datetime, timezone
from jose import jwt
from backend.main import _make_token, JWT_SECRET, JWT_ALGO, pwd_ctx, EmployeeIn, PHONE_RE, NAME_RE

def test_make_token():
    user_id = "test-user-id"
    token = _make_token(user_id)
    assert isinstance(token, str)
    
    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    assert payload["sub"] == user_id
    assert "exp" in payload

def test_password_hashing():
    password = "secret-password"
    hashed = pwd_ctx.hash(password)
    assert hashed != password
    assert pwd_ctx.verify(password, hashed) is True
    assert pwd_ctx.verify("wrong-password", hashed) is False

def test_regex_patterns():
    # Test Name Regex
    assert NAME_RE.match("John Doe")
    assert NAME_RE.match("A.B. Smith")
    assert not NAME_RE.match("A") # Too short
    assert not NAME_RE.match("John123") # Numbers not allowed

    # Test Phone Regex
    assert PHONE_RE.match("+1234567890")
    assert PHONE_RE.match("123-456-7890")
    assert PHONE_RE.match("(123) 456 7890")
    assert not PHONE_RE.match("abc") # Letters not allowed

def test_employee_model_validation():
    # Valid model
    data = {
        "full_name": "John Doe",
        "email": "john@example.com",
        "phone": "+1234567890",
        "position": "Dev",
        "department": "IT",
        "organization_id": "org-123",
        "joining_date": "2023-01-01",
        "salary": 50000.0,
        "status": "Active"
    }
    emp = EmployeeIn(**data)
    assert emp.full_name == "John Doe"

    # Invalid name
    data_bad_name = data.copy()
    data_bad_name["full_name"] = "J"
    with pytest.raises(ValueError, match="Name must be at least 2 chars"):
        EmployeeIn(**data_bad_name)

    # Invalid status
    data_bad_status = data.copy()
    data_bad_status["status"] = "Unknown"
    with pytest.raises(ValueError, match="Status must be Active or Inactive"):
        EmployeeIn(**data_bad_status)

    # Negative salary
    data_bad_salary = data.copy()
    data_bad_salary["salary"] = -100.0
    with pytest.raises(ValueError, match="Salary must be positive"):
        EmployeeIn(**data_bad_salary)
