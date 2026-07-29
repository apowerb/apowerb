from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr


class UserBase(BaseModel):
    """
    Shared properties for a User
    """

    first_name: str
    last_name: str
    email: EmailStr



class UserCreate(UserBase):
    """
    Schema for creating a new User
    inherits from UserBase, can add extra fields if needed.
    """

    password: str | None = None

    # Optional fields for OAuth users (can be null for regular users)
    username: str | None = None
    full_name: str | None = None
    avatar_url: str | None = None

    # GitHub fields
    github_id: str | None = None
    github_login: str | None = None
    github_access_token: str | None = None

    # Google fields (NEW)
    google_id: str | None = None
    google_access_token: str | None = None

    # Microsoft fields (NEW)
    microsoft_id: str | None = None
    microsoft_access_token: str | None = None

    # LinkedIn fields
    linkedin_id: str | None = None
    linkedin_access_token: str | None = None


class User(UserBase):
    """
    Schema for reading a User (e.g GET requests).
    inculdes DB-generated fields like IDs, timestamps, etc.
    """

    user_id: int
    created_at: datetime | None = None
    updated_at: datetime | None = None
    role: str
    username: str | None = None
    full_name: str | None = None
    avatar_url: str | None = None
    plan: str | None = None
    stripe_customer_id: str | None = None
    mfa_enabled: bool = False
    onboarding_completed: bool = False

    model_config = ConfigDict(from_attributes=True)


User.model_rebuild()


class UserUpdate(BaseModel):
    """Schema for partial user updates (PATCH)."""
    first_name: str | None = None
    onboarding_completed: bool | None = None
    last_name: str | None = None
    username: str | None = None
    full_name: str | None = None
