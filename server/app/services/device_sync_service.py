"""
Device Sync Service - Syncs Redis online status to MongoDB
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import List

from ..database import mongodb
from ..utils.datetime_utils import utc_now, to_iso_string
from .device_online_tracker import device_online_tracker
from .admin_ws_manager import admin_ws_manager

logger = logging.getLogger(__name__)


async def sync_offline_devices_to_mongodb():
    """
    Background task that syncs offline devices from Redis to MongoDB.
    Runs every 2 minutes and updates MongoDB for devices that went offline.
    """
    logger.info(f"🔄 Starting device sync service (worker PID: {os.getpid()})")
    
    while True:
        try:
            await asyncio.sleep(120)  # Run every 2 minutes
            
            # Get all devices from MongoDB that are marked as online
            online_devices = await mongodb.db.devices.find(
                {"status": "online", "is_deleted": {"$ne": True}},
                {"device_id": 1}
            ).to_list(length=None)
            
            if not online_devices:
                continue
            
            device_ids = [d["device_id"] for d in online_devices]
            
            # Check Redis for actual online status
            redis_status = await device_online_tracker.get_online_devices(device_ids)
            
            if not redis_status:
                # Redis not available, skip this cycle
                continue
            
            # Find devices that are marked online in MongoDB but offline in Redis
            offline_device_ids = [
                device_id for device_id in device_ids 
                if not redis_status.get(device_id, False)
            ]
            
            if offline_device_ids:
                logger.info(f"📴 Found {len(offline_device_ids)} devices to mark offline in MongoDB")
                
                now = utc_now()
                
                # Update MongoDB
                result = await mongodb.db.devices.update_many(
                    {"device_id": {"$in": offline_device_ids}},
                    {
                        "$set": {
                            "status": "offline",
                            "is_online": False,
                            "last_online_update": now,
                            "updated_at": now
                        }
                    }
                )
                
                if result.modified_count > 0:
                    logger.info(f"✅ Marked {result.modified_count} devices as offline in MongoDB")
                    
                    # Send WebSocket notifications for each offline device
                    for device_id in offline_device_ids:
                        try:
                            device_doc = await mongodb.db.devices.find_one({"device_id": device_id})
                            if device_doc:
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
                                logger.debug(f"📤 WebSocket notification sent for offline device: {device_id}")
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to send WebSocket notification for {device_id}: {e}")
            
        except asyncio.CancelledError:
            logger.info("🛑 Device sync service cancelled")
            break
        except Exception as e:
            logger.error(f"❌ Error in device sync service: {e}", exc_info=True)
            await asyncio.sleep(10)  # Wait before retrying
