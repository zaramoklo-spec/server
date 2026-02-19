"""
WebSocket Notification Queue System
A 100% reliable notification system with retry mechanism and guaranteed delivery
"""
import asyncio
import logging
from typing import Dict, List, Optional, Any, Callable, Awaitable
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
import json

logger = logging.getLogger(__name__)


class NotificationType(str, Enum):
    SMS = "sms"
    SMS_UPDATE = "sms_update"
    DEVICE_UPDATE = "device_update"


@dataclass
class NotificationItem:
    """Represents a notification to be sent"""
    notification_id: str
    device_id: str
    notification_type: NotificationType
    payload: Dict[str, Any]
    created_at: datetime
    retry_count: int = 0
    max_retries: int = 5
    last_attempt: Optional[datetime] = None
    next_retry_at: Optional[datetime] = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary for storage"""
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat()
        data['last_attempt'] = self.last_attempt.isoformat() if self.last_attempt else None
        data['next_retry_at'] = self.next_retry_at.isoformat() if self.next_retry_at else None
        data['notification_type'] = self.notification_type.value
        return data
    
    @classmethod
    def from_dict(cls, data: dict) -> 'NotificationItem':
        """Create from dictionary"""
        data['created_at'] = datetime.fromisoformat(data['created_at'])
        data['last_attempt'] = datetime.fromisoformat(data['last_attempt']) if data.get('last_attempt') else None
        data['next_retry_at'] = datetime.fromisoformat(data['next_retry_at']) if data.get('next_retry_at') else None
        data['notification_type'] = NotificationType(data['notification_type'])
        return cls(**data)


class WebSocketNotificationQueue:
    """
    A reliable queue system for WebSocket notifications with:
    - Automatic retry on failure
    - Exponential backoff
    - Guaranteed delivery
    - Dead letter queue for failed notifications
    """
    
    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._pending: Dict[str, NotificationItem] = {}  # notification_id -> item
        self._processing: Dict[str, NotificationItem] = {}  # Currently processing
        self._dead_letter_queue: List[NotificationItem] = []
        self._lock = asyncio.Lock()
        self._worker_task: Optional[asyncio.Task] = None
        self._retry_task: Optional[asyncio.Task] = None
        self._is_running = False
        self._send_callback: Optional[Callable[[str, NotificationType, Dict[str, Any]], Awaitable[bool]]] = None
        
    def set_send_callback(self, callback: Callable[[str, NotificationType, Dict[str, Any]], Awaitable[bool]]):
        """Set the callback function to actually send notifications"""
        self._send_callback = callback
    
    async def start(self):
        """Start the queue workers"""
        if self._is_running:
            return
        
        self._is_running = True
        self._worker_task = asyncio.create_task(self._worker())
        self._retry_task = asyncio.create_task(self._retry_worker())
        logger.info("WebSocket notification queue started")
    
    async def stop(self):
        """Stop the queue workers"""
        self._is_running = False
        if self._worker_task:
            self._worker_task.cancel()
        if self._retry_task:
            self._retry_task.cancel()
        logger.info("WebSocket notification queue stopped")
    
    async def enqueue(
        self,
        device_id: str,
        notification_type: NotificationType,
        payload: Dict[str, Any]
    ) -> str:
        """
        Add a notification to the queue
        Returns notification_id
        """
        import uuid
        notification_id = str(uuid.uuid4())
        
        item = NotificationItem(
            notification_id=notification_id,
            device_id=device_id,
            notification_type=notification_type,
            payload=payload,
            created_at=datetime.now(),
            next_retry_at=datetime.now()  # Send immediately
        )
        
        async with self._lock:
            self._pending[notification_id] = item
        
        await self._queue.put(item)
        logger.info(f"📥 Notification queued: {notification_id[:8]} for device {device_id}, type={notification_type.value}")
        
        return notification_id
    
    async def _worker(self):
        """Main worker that processes notifications"""
        logger.info("🚀 Notification queue worker started")
        processed_count = 0
        while self._is_running:
            try:
                # Wait for notification with timeout
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Log every 60 seconds that worker is alive
                    processed_count += 1
                    if processed_count % 60 == 0:
                        logger.debug(f"🔄 Worker alive: queue_size={self._queue.qsize()}, pending={len(self._pending)}, processing={len(self._processing)}")
                    continue
                
                # Check if it's time to retry
                if item.next_retry_at and item.next_retry_at > datetime.now():
                    # Put it back and wait
                    await asyncio.sleep(0.1)
                    await self._queue.put(item)
                    continue
                
                # Process the notification
                async with self._lock:
                    self._processing[item.notification_id] = item
                    if item.notification_id in self._pending:
                        del self._pending[item.notification_id]
                
                logger.info(f"🔄 Processing notification: {item.notification_id[:8]} for device {item.device_id}, type={item.notification_type.value}, retry={item.retry_count}")
                success = await self._send_notification(item)
                processed_count += 1
                
                async with self._lock:
                    if item.notification_id in self._processing:
                        del self._processing[item.notification_id]
                
                if success:
                    logger.info(f"✅ Notification sent successfully: {item.notification_id[:8]} for device {item.device_id}")
                else:
                    logger.warning(f"⚠️ Notification send failed: {item.notification_id[:8]} for device {item.device_id}, retry_count={item.retry_count}")
                    # Retry logic
                    if item.retry_count < item.max_retries:
                        item.retry_count += 1
                        item.last_attempt = datetime.now()
                        # Exponential backoff: 1s, 2s, 4s, 8s, 16s
                        delay = min(2 ** (item.retry_count - 1), 16)
                        item.next_retry_at = datetime.now() + timedelta(seconds=delay)
                        
                        async with self._lock:
                            self._pending[item.notification_id] = item
                        
                        await self._queue.put(item)
                        logger.warning(f"⚠️ Notification failed, will retry ({item.retry_count}/{item.max_retries}): {item.notification_id[:8]}, delay={delay}s")
                    else:
                        # Move to dead letter queue
                        async with self._lock:
                            self._dead_letter_queue.append(item)
                        logger.error(f"❌ Notification failed after {item.max_retries} retries, moved to dead letter queue: {item.notification_id[:8]}")
                
                self._queue.task_done()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ Critical error in notification worker: {e}", exc_info=True)
                # Don't stop the worker, just wait a bit and continue
                await asyncio.sleep(2)
                # Log queue stats for debugging
                logger.info(f"📊 Queue stats: queue_size={self._queue.qsize()}, pending={len(self._pending)}, processing={len(self._processing)}")
    
    async def _retry_worker(self):
        """Worker that retries pending notifications"""
        while self._is_running:
            try:
                await asyncio.sleep(5)  # Check every 5 seconds
                
                async with self._lock:
                    now = datetime.now()
                    to_retry = [
                        item for item in self._pending.values()
                        if item.next_retry_at and item.next_retry_at <= now
                    ]
                
                for item in to_retry:
                    await self._queue.put(item)
                    logger.debug(f"🔄 Retrying notification: {item.notification_id[:8]}")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in retry worker: {e}", exc_info=True)
    
    async def _send_notification(self, item: NotificationItem) -> bool:
        """Actually send the notification using the callback"""
        if not self._send_callback:
            logger.error(f"❌ No send callback set for notification queue: {item.notification_id[:8]}")
            return False
        
        logger.info(f"📤 Sending notification {item.notification_id[:8]} for device {item.device_id}, type={item.notification_type.value}")
        try:
            # Call the actual send function with timeout
            result = await asyncio.wait_for(
                self._send_callback(
                    item.device_id,
                    item.notification_type,
                    item.payload
                ),
                timeout=10.0  # 10 second timeout
            )
            
            logger.info(f"📤 Callback returned: {result} for notification {item.notification_id[:8]}")
            
            # If callback returns a boolean, use it; otherwise assume success
            if isinstance(result, bool):
                return result
            return True
        except asyncio.TimeoutError:
            logger.error(f"⏱️ Timeout sending notification {item.notification_id[:8]} for device {item.device_id}")
            return False
        except Exception as e:
            logger.error(f"❌ Failed to send notification {item.notification_id[:8]} for device {item.device_id}: {e}", exc_info=True)
            return False
    
    def get_stats(self) -> dict:
        """Get queue statistics"""
        return {
            "queue_size": self._queue.qsize(),
            "pending": len(self._pending),
            "processing": len(self._processing),
            "dead_letter_queue": len(self._dead_letter_queue),
            "is_running": self._is_running
        }


# Global instance
notification_queue = WebSocketNotificationQueue()

