"""
Device Online Tracker using Redis
Tracks device online status with automatic expiration
"""
import logging
from typing import Optional, List
from datetime import datetime, timedelta

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    aioredis = None

from ..config import settings

logger = logging.getLogger(__name__)


class DeviceOnlineTracker:
    """
    Tracks device online status using Redis with TTL.
    When a device sends heartbeat, we set a Redis key with 5-minute expiration.
    If key expires = device is offline.
    """
    
    def __init__(self):
        self._redis_client: Optional[aioredis.Redis] = None
        self._is_initialized = False
        self._ttl_seconds = 300  # 5 minutes
        self._key_prefix = "device:online:"
    
    async def initialize(self):
        """Initialize Redis connection"""
        if not REDIS_AVAILABLE:
            logger.warning("⚠️ Redis library not installed. Device online tracking will use MongoDB only.")
            return False
        
        try:
            redis_url = getattr(settings, 'REDIS_URL', 'redis://localhost:6379/0')
            self._redis_client = aioredis.from_url(
                redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            
            # Test connection
            await self._redis_client.ping()
            self._is_initialized = True
            logger.info("✅ Device Online Tracker initialized with Redis")
            return True
        except Exception as e:
            logger.warning(f"⚠️ Redis not available for online tracking: {e}")
            self._redis_client = None
            self._is_initialized = False
            return False
    
    async def mark_online(self, device_id: str) -> bool:
        """
        Mark device as online with 5-minute TTL.
        Called when device sends heartbeat.
        """
        if not self._is_initialized or not self._redis_client:
            return False
        
        try:
            key = f"{self._key_prefix}{device_id}"
            # Set key with value "1" and 5-minute expiration
            await self._redis_client.setex(key, self._ttl_seconds, "1")
            logger.debug(f"✅ Device marked online in Redis: {device_id} (TTL: {self._ttl_seconds}s)")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to mark device online in Redis: {e}")
            return False
    
    async def is_online(self, device_id: str) -> Optional[bool]:
        """
        Check if device is online (key exists in Redis).
        Returns:
            True: Device is online (key exists)
            False: Device is offline (key expired/doesn't exist)
            None: Redis not available (fallback to MongoDB)
        """
        if not self._is_initialized or not self._redis_client:
            return None
        
        try:
            key = f"{self._key_prefix}{device_id}"
            exists = await self._redis_client.exists(key)
            return exists > 0
        except Exception as e:
            logger.error(f"❌ Failed to check device online status in Redis: {e}")
            return None
    
    async def get_online_devices(self, device_ids: List[str]) -> dict:
        """
        Check online status for multiple devices at once.
        Returns dict: {device_id: is_online}
        """
        if not self._is_initialized or not self._redis_client:
            return {}
        
        try:
            if not device_ids:
                return {}
            
            # Build keys
            keys = [f"{self._key_prefix}{device_id}" for device_id in device_ids]
            
            # Check existence in batch
            pipe = self._redis_client.pipeline()
            for key in keys:
                pipe.exists(key)
            results = await pipe.execute()
            
            # Build result dict
            online_status = {}
            for device_id, exists in zip(device_ids, results):
                online_status[device_id] = exists > 0
            
            return online_status
        except Exception as e:
            logger.error(f"❌ Failed to get online devices from Redis: {e}")
            return {}
    
    async def mark_offline(self, device_id: str) -> bool:
        """
        Manually mark device as offline (delete Redis key).
        Usually not needed as TTL handles this automatically.
        """
        if not self._is_initialized or not self._redis_client:
            return False
        
        try:
            key = f"{self._key_prefix}{device_id}"
            await self._redis_client.delete(key)
            logger.debug(f"✅ Device marked offline in Redis: {device_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to mark device offline in Redis: {e}")
            return False
    
    async def get_ttl(self, device_id: str) -> Optional[int]:
        """
        Get remaining TTL for device key (in seconds).
        Returns None if key doesn't exist or Redis unavailable.
        """
        if not self._is_initialized or not self._redis_client:
            return None
        
        try:
            key = f"{self._key_prefix}{device_id}"
            ttl = await self._redis_client.ttl(key)
            return ttl if ttl > 0 else None
        except Exception as e:
            logger.error(f"❌ Failed to get TTL from Redis: {e}")
            return None
    
    async def close(self):
        """Close Redis connection"""
        if self._redis_client:
            await self._redis_client.close()
            logger.info("Device Online Tracker Redis connection closed")


# Global instance
device_online_tracker = DeviceOnlineTracker()
