from typing import Optional, Dict, Any, Literal
from pydantic import BaseModel, Field


class LeakLookupRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=256)
    limit: int = Field(100, ge=1, le=10000)
    lang: str = Field("en", min_length=2, max_length=10)
    response_type: Literal["json", "short", "html"] = "json"
    bot_name: Optional[str] = None


class LeakLookupResponse(BaseModel):
    success: bool = True
    data: Dict[str, Any]





