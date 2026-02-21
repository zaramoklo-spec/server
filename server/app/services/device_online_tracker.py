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
    
    Now uses centralized connection pool for better performance.
    """
    
    def __init__(self):
        self._redis_client: Optional[aioredis.Redis] = None
        self._is_initialized = False
        self._ttl_seconds = 300  # 5 minutes
        self._key_prefix = "device:online:"
    
    async def initialize(self):
        """Initialize Redis connection using connection pool"""
        if not REDIS_AVAILABLE:
            logger.warning("⚠️ Redis library not installed. Device online tracking will use MongoDB only.")
            return False
        
        try:
            # Use centralized connection pool
            from .redis_connection_pool import redis_manager
            
            if not redis_manager.is_available:
                await redis_manager.initialize()
            
            self._redis_client = redis_manager.get_client()
            
            if self._redis_client:
                # Test connection
                await self._redis_client.ping()
                self._is_initialized = True
                logger.info("✅ Device Online Tracker initialized with Redis connection pool")
                return True
            else:
                logger.warning("⚠️ Redis connection pool not available")
                return False
                
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
        Check online status for multiple devices at once (batch operation).
        Returns dict: {device_id: is_online}
        
        Uses Redis pipeline for optimal performance.
        """
        if not self._is_initialized or not self._redis_client:
            return {}
        
        try:
            if not device_ids:
                return {}
            
            # Build keys
            keys = [f"{self._key_prefix}{device_id}" for device_id in device_ids]
            
            # Check existence in batch using pipeline (much faster!)
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
    
    async def get_all_online_device_ids(self) -> List[str]:
        """
        Get all currently online device IDs.
        Useful for monitoring and statistics.
        """
        if not self._is_initialized or not self._redis_client:
            return []
        
        try:
            pattern = f"{self._key_prefix}*"
            keys = []
            
            # Use SCAN instead of KEYS for better performance
            cursor = 0
            while True:
                cursor, batch = await self._redis_client.scan(
                    cursor=cursor,
                    match=pattern,
                    count=100
                )
                keys.extend(batch)
                if cursor == 0:
                    break
            
            # Extract device IDs from keys
            device_ids = [key.replace(self._key_prefix, "") for key in keys]
            return device_ids
            
        except Exception as e:
            logger.error(f"❌ Failed to get all online devices: {e}")
            return []
    
    async def close(self):
        """Close Redis connection (handled by connection pool)"""
        # Connection pool handles cleanup
        logger.info("Device Online Tracker closed (connection pool manages connections)")


# Global instance
device_online_tracker = DeviceOnlineTracker()
