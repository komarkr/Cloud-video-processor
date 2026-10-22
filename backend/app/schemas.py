from datetime import datetime
import uuid
from typing import Any, Dict, Optional
from pydantic import BaseModel, EmailStr, Field


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6, description="Password (at least 6 characters)")


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    is_admin: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageResponse(BaseModel):
    status: str
    message: str
    user: Optional[UserOut] = None


class TaskOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    status: str
    operation_type: str
    params: Optional[Dict[str, Any]] = None
    input_file: str
    output_file: Optional[str] = None
    duration_sec: Optional[float] = None
    queue_position: Optional[int] = None
    eta_sec: Optional[int] = None
    error_message: Optional[str] = None
    worker_id: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
