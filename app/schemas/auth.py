from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class UserRole(str, Enum):
    super_admin = "super_admin"
    doctor = "doctor"
    user = "user"
    auditor = "auditor"


class AuthUserResponse(BaseModel):
    id: int
    username: str
    role: UserRole
    is_active: bool


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: AuthUserResponse


class LogoutResponse(BaseModel):
    logged_out: bool


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=8, max_length=200)
    role: UserRole


class UpdatePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)
    confirm_password: str = Field(min_length=8, max_length=200)

    @field_validator("confirm_password")
    @classmethod
    def _confirm_matches(cls, value: str, info):
        new_password = info.data.get("new_password")
        if new_password is not None and value != new_password:
            raise ValueError("confirm_password must match new_password")
        return value


class UserListResponse(BaseModel):
    total: int
    items: list[AuthUserResponse]

class UserBankDetailsItem(BaseModel):
    user_id: int
    username: str
    role: UserRole
    user_is_active: bool
    account_holder_name: str
    bank_name: str
    branch_name: str
    account_number: str
    payment_rate: str
    ifsc_code: str
    upi_id: str
    notes: str
    bank_is_active: bool
    updated_by: str
    updated_at: datetime | None


class UserBankDetailsListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[UserBankDetailsItem]


class UserBankDetailsUpsertRequest(BaseModel):
    account_holder_name: str = Field(default="", max_length=255)
    bank_name: str = Field(default="", max_length=255)
    branch_name: str = Field(default="", max_length=255)
    account_number: str = Field(default="", max_length=64)
    payment_rate: str = Field(default="", max_length=64)
    ifsc_code: str = Field(default="", max_length=32)
    upi_id: str = Field(default="", max_length=255)
    notes: str = Field(default="", max_length=2000)
    is_active: bool = True

class IfscVerificationResponse(BaseModel):
    ifsc_code: str
    valid: bool = True
    bank_name: str
    branch_name: str
    address: str
    city: str
    district: str
    state: str
    contact: str
    micr: str
    bank_code: str
    upi: bool | None = None
    neft: bool | None = None
    rtgs: bool | None = None
    imps: bool | None = None
    source: str = "razorpay_ifsc"
    raw: dict[str, Any] = Field(default_factory=dict)


class GstVerificationResponse(BaseModel):
    gstin: str
    valid: bool = False
    legal_name: str = ""
    trade_name: str = ""
    gst_status: str = ""
    is_active: bool | None = None
    taxpayer_type: str = ""
    constitution_of_business: str = ""
    registration_date: str = ""
    cancellation_date: str = ""
    state_jurisdiction: str = ""
    center_jurisdiction: str = ""
    principal_address: str = ""
    pincode: str = ""
    state: str = ""
    nature_of_business: list[str] = Field(default_factory=list)
    is_pharmacy: bool = False
    pharmacy_match_reason: str = ""
    message: str = ""
    source: str = "cleartax_gst_search"
    upstream_status_code: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)



