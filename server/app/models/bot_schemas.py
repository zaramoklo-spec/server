from pydantic import BaseModel
from typing import Optional

class BotOTPRequest(BaseModel):
    username: str
    bot_identifier: str

class BotOTPVerify(BaseModel):
    username: str
    otp_code: str
    bot_identifier: str

class BotOTPResponse(BaseModel):
    success: bool
    message: str
    expires_in: int = 300

class BotTokenResponse(BaseModel):
    success: bool
    message: str
    service_token: str
    token_type: str = "bearer"
    admin_info: dict

class BotStatusResponse(BaseModel):
    active: bool
    admin_username: str
    device_token: Optional[str] = None
    message: Optional[str] = None