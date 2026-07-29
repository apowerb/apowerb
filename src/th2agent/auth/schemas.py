from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    email: str
    password: str


class Token(BaseModel):
    token_type: str
    access_token: str


class TokenData(BaseModel):
    email: str | None = None


# -- MFA Schemas --------------------------------------------------------------


class MfaSetupResponse(BaseModel):
    secret: str
    qr_code_uri: str
    qr_code_base64: str


class MfaVerifyRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=20)  # W-04: validation


class MfaEnableRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=20)  # C-03: secret removed, W-04: validation


class MfaDisableRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=20)  # W-04: validation


class MfaLoginRequest(BaseModel):
    mfa_token: str
    code: str = Field(..., min_length=6, max_length=20)  # W-04: validation


class MfaLoginResponse(BaseModel):
    mfa_required: bool = True
    mfa_token: str


# -- Forgot / reset password (B18) -------------------------------------------


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8, max_length=256)


class VerifyEmailRequest(BaseModel):
    token: str


class ResendVerificationRequest(BaseModel):
    email: str
