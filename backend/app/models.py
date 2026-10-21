import enum
import uuid
from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func
from app.db import Base


class TaskStatus(str, enum.Enum):
  queued = "queued"
  running = "running"
  done = "done"
  failed = "failed"
  cancelled = "cancelled"


class User(Base):
  __tablename__ = "users"

  id = Column(
      UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True
  )
  email = Column(String, unique=True, index=True, nullable=False)
  hashed_password = Column(String, nullable=False)
  created_at = Column(
      DateTime(timezone=True), server_default=func.now(), nullable=False
  )


class Task(Base):
  __tablename__ = "tasks"

  id = Column(
      UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True
  )
  user_id = Column(
      UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
  )
  status = Column(
      Enum(TaskStatus, name="task_status_enum"),
      default=TaskStatus.queued,
      nullable=False,
  )
  operation_type = Column(
      String, nullable=False
  )  # наприклад: convert, scale, watermark тощо
  params = Column(JSONB, nullable=True)  # параметри операції
  input_file = Column(String, nullable=False)
  output_file = Column(String, nullable=True)
  error_message = Column(Text, nullable=True)
  created_at = Column(
      DateTime(timezone=True), server_default=func.now(), nullable=False
  )
  updated_at = Column(
      DateTime(timezone=True),
      server_default=func.now(),
      onupdate=func.now(),
      nullable=False,
  )