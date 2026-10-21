from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User
from app.routers.deps import get_current_user, require_admin
from app.schemas import MessageResponse, UserLogin, UserOut, UserRegister
from app.services.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api", tags=["auth"])


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
)
def register(
    user_in: UserRegister,
    db: Session = Depends(get_db),
):
    """Register a new user account with duplicate email check."""
    existing_user = db.query(User).filter(User.email == user_in.email).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    new_user = User(
        email=user_in.email,
        hashed_password=hash_password(user_in.password),
        is_admin=False,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


@router.post(
    "/login",
    response_model=MessageResponse,
    summary="Login and receive HttpOnly JWT cookie",
)
def login(
    user_in: UserLogin,
    response: Response,
    db: Session = Depends(get_db),
):
    """Authenticate user and set secure HttpOnly JWT cookie."""
    user = db.query(User).filter(User.email == user_in.email).first()
    if not user or not verify_password(user_in.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    token = create_access_token(
        data={
            "sub": str(user.id),
            "email": user.email,
            "is_admin": user.is_admin,
        }
    )

    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        max_age=settings.jwt_expire_minutes * 60,
        path="/",
    )

    return MessageResponse(
        status="ok",
        message="Logged in successfully",
        user=UserOut.model_validate(user),
    )


@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="Logout and clear HttpOnly cookie",
)
def logout(response: Response):
    """Clear HttpOnly authentication cookie."""
    response.delete_cookie(
        key="access_token",
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )
    return MessageResponse(status="ok", message="Logged out successfully")


@router.get(
    "/me",
    response_model=UserOut,
    summary="Get current authenticated user profile",
)
def get_me(current_user: User = Depends(get_current_user)):
    """Return profile of the currently logged-in user."""
    return current_user


@router.get(
    "/admin/check",
    summary="Check admin authorization (403 for non-admins)",
)
def admin_only_check(admin: User = Depends(require_admin)):
    """Admin-only endpoint to verify admin privileges."""
    return {
        "status": "ok",
        "message": "Admin privileges confirmed",
        "admin_email": admin.email,
    }
