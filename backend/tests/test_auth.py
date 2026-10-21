import pytest
from app.config import settings


def test_register_success(client):
    """Test successful user registration."""
    response = client.post(
        "/api/register",
        json={"email": "newuser@example.com", "password": "securepassword123"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "newuser@example.com"
    assert "id" in data
    assert data["is_admin"] is False
    assert "hashed_password" not in data


def test_register_duplicate_email(client):
    """Test that registering an already existing email returns 400."""
    user_payload = {"email": "duplicate@example.com", "password": "password123"}
    # First registration
    res1 = client.post("/api/register", json=user_payload)
    assert res1.status_code == 201

    # Second registration with same email
    res2 = client.post("/api/register", json=user_payload)
    assert res2.status_code == 400
    assert "already registered" in res2.json()["detail"].lower()


def test_login_success(client):
    """Test login sets HttpOnly, Secure, SameSite=Lax cookie."""
    # Register user first
    client.post(
        "/api/register",
        json={"email": "loginuser@example.com", "password": "password123"},
    )

    # Login
    response = client.post(
        "/api/login",
        json={"email": "loginuser@example.com", "password": "password123"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "user" in data

    # Verify cookie attributes
    set_cookie = response.headers.get("set-cookie")
    assert set_cookie is not None
    assert "access_token=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert "secure" in set_cookie.lower()


def test_login_wrong_password(client):
    """Test that login with incorrect password returns 401."""
    client.post(
        "/api/register",
        json={"email": "wrongpwd@example.com", "password": "correctpassword"},
    )

    response = client.post(
        "/api/login",
        json={"email": "wrongpwd@example.com", "password": "incorrectpassword"},
    )
    assert response.status_code == 401
    assert "invalid" in response.json()["detail"].lower()


def test_protected_me_without_cookie_returns_401(client):
    """Test accessing protected /api/me without cookie returns 401."""
    response = client.get("/api/me")
    assert response.status_code == 401
    assert "not authenticated" in response.json()["detail"].lower()


def test_protected_me_accessible_after_login(client):
    """Test protected endpoint /api/me works only after login with valid cookie."""
    # Register and login
    email = "authenticated@example.com"
    pwd = "password123"
    client.post("/api/register", json={"email": email, "password": pwd})
    login_res = client.post("/api/login", json={"email": email, "password": pwd})
    assert login_res.status_code == 200

    # Test /api/me works automatically using the stored cookie in client
    me_response = client.get("/api/me")
    assert me_response.status_code == 200
    data = me_response.json()
    assert data["email"] == email
    assert data["is_admin"] is False


def test_require_admin_forbidden_for_regular_user(client):
    """Test that a regular (non-admin) user gets 403 Forbidden on admin endpoint."""
    email = "regular@example.com"
    pwd = "password123"
    client.post("/api/register", json={"email": email, "password": pwd})
    client.post("/api/login", json={"email": email, "password": pwd})

    # Access admin endpoint
    response = client.get("/api/admin/check")
    assert response.status_code == 403
    assert "admin privileges required" in response.json()["detail"].lower()


def test_require_admin_allowed_for_admin_user(client):
    """Test that pre-seeded admin user from ADMIN_EMAIL / ADMIN_PASSWORD can access admin endpoint."""
    login_res = client.post(
        "/api/login",
        json={
            "email": settings.admin_email,
            "password": settings.admin_password,
        },
    )
    assert login_res.status_code == 200

    # Access admin endpoint
    response = client.get("/api/admin/check")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["admin_email"] == settings.admin_email


def test_logout_clears_cookie_and_revokes_access(client):
    """Test logout clears the access_token cookie and revokes /api/me access."""
    email = "logoutuser@example.com"
    pwd = "password123"
    client.post("/api/register", json={"email": email, "password": pwd})
    client.post("/api/login", json={"email": email, "password": pwd})

    # Verify authenticated
    assert client.get("/api/me").status_code == 200

    # Logout
    logout_res = client.post("/api/logout")
    assert logout_res.status_code == 200

    # Verify cookie is cleared in response
    set_cookie = logout_res.headers.get("set-cookie")
    assert set_cookie is not None

    # After logout, /api/me should return 401
    me_after_logout = client.get("/api/me")
    assert me_after_logout.status_code == 401
