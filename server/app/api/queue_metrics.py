"""
API endpoints for FCM Queue metrics and monitoring
"""
from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any, List
import logging
import asyncio

from ..services.fcm_queue_service import fcm_queue_service, Priority
from ..services.device_service import device_service
from ..database import mongodb
from ..models.admin_schemas import Admin
from ..utils.auth_middleware import require_permission, AdminPermission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/queue", tags=["queue"])


@router.get("/metrics")
async def get_queue_metrics() -> Dict[str, Any]:
    """Get FCM queue metrics"""
    try:
        metrics = await fcm_queue_service.get_metrics()
        queue_length = await fcm_queue_service.get_queue_length()
        
        return {
            "success": True,
            "queue_length": queue_length,
            "metrics": metrics
        }
    except Exception as e:
        logger.error(f"❌ Failed to get metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def get_queue_status() -> Dict[str, Any]:
    """Get queue status"""
    try:
        queue_length = await fcm_queue_service.get_queue_length()
        is_initialized = fcm_queue_service._is_initialized
        
        return {
            "success": True,
            "initialized": is_initialized,
            "queue_length": queue_length,
            "batch_size": fcm_queue_service.BATCH_SIZE,
            "max_rate_per_second": fcm_queue_service.MAX_RATE_PER_SECOND
        }
    except Exception as e:
        logger.error(f"❌ Failed to get status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ping-all")
async def ping_all_devices(
    priority: str = "normal",
    current_admin: Admin = Depends(require_permission(AdminPermission.VIEW_DEVICES))
) -> Dict[str, Any]:
    """
    Send FCM ping to ALL devices
    Enqueues all device IDs to FCM queue for processing
    """
    try:
        # Initialize queue if not already initialized
        if not fcm_queue_service._is_initialized:
            await fcm_queue_service.initialize()
        
        if not fcm_queue_service._is_initialized:
            raise HTTPException(status_code=500, detail="FCM Queue Service not available")
        
        # Get all device IDs from MongoDB
        devices = await mongodb.db.devices.find(
            {
                "is_deleted": {"$ne": True},
                "model": {"$exists": True, "$ne": None}
            },
            {"device_id": 1}
        ).to_list(length=None)
        
        device_ids = [d["device_id"] for d in devices if d.get("device_id")]
        
        if not device_ids:
            return {
                "success": True,
                "message": "No devices found",
                "enqueued": 0,
                "total_devices": 0
            }
        
        # Map priority string to Priority enum
        priority_map = {
            "critical": Priority.CRITICAL,
            "high": Priority.HIGH,
            "normal": Priority.NORMAL,
            "low": Priority.LOW
        }
        priority_enum = priority_map.get(priority.lower(), Priority.NORMAL)
        
        # Enqueue all devices
        enqueued = await fcm_queue_service.enqueue(
            device_ids=device_ids,
            priority=priority_enum,
            metadata={
                "triggered_by": current_admin.username,
                "reason": "manual_ping_all",
                "total_devices": len(device_ids)
            }
        )
        
        logger.info(f"📥 {current_admin.username} enqueued {enqueued} devices for FCM ping (priority: {priority_enum.name})")
        
        return {
            "success": True,
            "message": f"Enqueued {enqueued} devices for FCM ping",
            "enqueued": enqueued,
            "total_devices": len(device_ids),
            "priority": priority_enum.name
        }
        
    except Exception as e:
        logger.error(f"❌ Failed to ping all devices: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

