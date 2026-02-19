from pydantic import BaseModel
from typing import Optional

class OTPRequest(BaseModel):
    username: str
    password: str

class OTPVerify(BaseModel):
    username: str
    otp_code: str
    temp_token: str
    fcm_token: Optional[str] = None

class OTPResponse(BaseModel):
    success: bool
    message: str
    temp_token: Optional[str] = None
    expires_in: Optional[int] = None

class OTPVerifyResponse(BaseModel):
    success: bool
    message: str
    access_token: Optional[str] = None
    token_type: Optional[str] = "bearer"
    expires_in: Optional[int] = None
    admin: Optional[dict] = None