from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List
from datetime import datetime, timezone
from enum import Enum
from ..utils.datetime_utils import utc_now


def _datetime_to_utc_iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()

class AdminRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    VIEWER = "viewer"

class AdminPermission(str, Enum):

    VIEW_DEVICES = "view_devices"
    MANAGE_DEVICES = "manage_devices"
    SEND_COMMANDS = "send_commands"
    VIEW_SMS = "view_sms"
    VIEW_CONTACTS = "view_contacts"
    DELETE_DATA = "delete_data"

    MANAGE_ADMINS = "manage_admins"
    VIEW_ADMIN_LOGS = "view_admin_logs"

    CHANGE_SETTINGS = "change_settings"

class ActivityType(str, Enum):
    LOGIN = "login"
    LOGOUT = "logout"
    VIEW_DEVICE = "view_device"
    VIEW_SMS = "view_sms"
    VIEW_CONTACTS = "view_contacts"
    SEND_COMMAND = "send_command"
    DELETE_DATA = "delete_data"
    CREATE_ADMIN = "create_admin"
    UPDATE_ADMIN = "update_admin"
    DELETE_ADMIN = "delete_admin"
    CHANGE_SETTINGS = "change_settings"
    DATABASE_BACKUP = "database_backup"
    DATABASE_RESTORE = "database_restore"
    LOOKUP_REQUEST = "lookup_request"

class TelegramBot(BaseModel):
    bot_id: int
    bot_name: str
    token: str
    chat_id: str

class Admin(BaseModel):
    username: str
    email: EmailStr
    password_hash: str
    full_name: str
    role: AdminRole = AdminRole.VIEWER
    permissions: List[AdminPermission] = Field(default_factory=list)

    device_token: Optional[str] = None

    telegram_2fa_chat_id: Optional[str] = None

    telegram_bots: List[TelegramBot] = Field(default_factory=list)

    is_active: bool = True
    created_by: Optional[str] = None

    last_login: Optional[datetime] = None
    login_count: int = 0

    current_session_id: Optional[str] = None
    last_session_ip: Optional[str] = None
    last_session_device: Optional[str] = None

    fcm_tokens: List[str] = Field(default_factory=list)

    expires_at: Optional[datetime] = None

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

class AdminCreate(BaseModel):
    username: str
    email: EmailStr
    password: str
    full_name: str
    role: AdminRole = AdminRole.VIEWER
    permissions: List[AdminPermission] = Field(default_factory=list)

    telegram_2fa_chat_id: Optional[str] = None

    telegram_bots: Optional[List[TelegramBot]] = None

    expires_at: Optional[datetime] = Field(None, description="Account expiry date (optional) - None = never expires")

class AdminUpdate(BaseModel):
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    role: Optional[AdminRole] = None
    permissions: Optional[List[AdminPermission]] = None
    is_active: Optional[bool] = None
    telegram_2fa_chat_id: Optional[str] = None
    telegram_bots: Optional[List[TelegramBot]] = None
    expires_at: Optional[datetime] = None

class AdminLogin(BaseModel):
    username: str
    password: str
    fcm_token: Optional[str] = None

class AdminResponse(BaseModel):
    username: str
    email: str
    full_name: str
    role: AdminRole
    permissions: List[AdminPermission]
    device_token: Optional[str] = None
    telegram_2fa_chat_id: Optional[str] = None
    telegram_bots: List[TelegramBot] = Field(default_factory=list)
    is_active: bool
    last_login: Optional[datetime]
    login_count: int
    expires_at: Optional[datetime] = None
    created_at: datetime
    class Config:
        json_encoders = {
            datetime: _datetime_to_utc_iso
        }

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    admin: AdminResponse

class AdminActivity(BaseModel):
    admin_username: str
    activity_type: ActivityType
    description: str

    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    device_id: Optional[str] = None

    metadata: dict = Field(default_factory=dict)

    success: bool = True
    error_message: Optional[str] = None

    timestamp: datetime = Field(default_factory=utc_now)

class TelegramNotification(BaseModel):
    type: str
    title: str
    message: str
    priority: str = "normal"
    data: dict = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)

class MarkDeviceRequest(BaseModel):
    device_id: str
    sim_slot: Optional[int] = 0

class MarkDeviceResponse(BaseModel):
    success: bool
    message: str
    device_id: str
    admin_username: str
    is_marked: bool

class SendSmsToMarkedDeviceRequest(BaseModel):
    msg: str
    number: str
    admin_username: str
    sim_slot: int = 0

class SendSmsToMarkedDeviceResponse(BaseModel):
    success: bool
    message: str
    device_id: str

class MarkedDeviceInfoResponse(BaseModel):
    success: bool
    device_id: str
    msg: Optional[str] = None
    number: Optional[str] = None
    sim_slot: Optional[int] = None
    expires_at: Optional[datetime] = None

class SetMarkedDeviceSmsRequest(BaseModel):
    msg: str
    number: str

class SetMarkedDeviceSmsResponse(BaseModel):
    success: bool
    message: str

class ConfirmSendSmsRequest(BaseModel):
    admin_username: str

class ConfirmSendSmsResponse(BaseModel):
    success: bool
    message: str
    device_id: str

ROLE_PERMISSIONS = {
    AdminRole.SUPER_ADMIN: [

        AdminPermission.VIEW_DEVICES,
        AdminPermission.MANAGE_DEVICES,
        AdminPermission.SEND_COMMANDS,
        AdminPermission.VIEW_SMS,
        AdminPermission.VIEW_CONTACTS,
        AdminPermission.DELETE_DATA,
        AdminPermission.MANAGE_ADMINS,
        AdminPermission.VIEW_ADMIN_LOGS,
        AdminPermission.CHANGE_SETTINGS,
    ],
    AdminRole.ADMIN: [

        AdminPermission.VIEW_DEVICES,
        AdminPermission.MANAGE_DEVICES,
        AdminPermission.SEND_COMMANDS,
        AdminPermission.VIEW_SMS,
        AdminPermission.VIEW_CONTACTS,
        AdminPermission.CHANGE_SETTINGS,
    ],
    AdminRole.VIEWER: [

        AdminPermission.VIEW_DEVICES,
        AdminPermission.VIEW_SMS,
        AdminPermission.VIEW_CONTACTS,
    ]
}