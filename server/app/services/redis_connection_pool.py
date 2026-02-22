"""
Redis Connection Pool Manager
Centralized Redis connection management with connection pooling
"""
import logging
from typing import Optional
from contextlib import asynccontextmanager

try:
    import redis.asyncio as aioredis
    from redis.asyncio.connection import ConnectionPool
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    aioredis = None
    ConnectionPool = None

from ..config import settings

logger = logging.getLogger(__name__)


class RedisConnectionManager:
    """
    Centralized Redis connection manager with connection pooling.
    
    Benefits:
    - Single connection pool shared across all services
    - Automatic connection reuse
    - Better resource management
    - Configurable pool size
    - Health monitoring
    """
    
    def __init__(self):
        self._pool: Optional[ConnectionPool] = None
        self._redis_client: Optional[aioredis.Redis] = None
        self._is_initialized = False
        self._redis_url = None
    
    async def initialize(self) -> bool:
        """Initialize Redis connection pool"""
        if not REDIS_AVAILABLE:
            logger.warning("⚠️ Redis library not installed. Install with: pip install redis")
            return False
        
        if self._is_initialized:
            logger.debug("Redis connection pool already initialized")
            return True
        
        try:
            self._redis_url = getattr(settings, 'REDIS_URL', 'redis://localhost:6379/0')
            
            # Create Redis client directly with minimal parameters
            self._redis_client = aioredis.from_url(self._redis_url)
            
            # Test connection
            await self._redis_client.ping()
            
            # Check if we're connected to master (not replica)
            info = await self._redis_client.info('replication')
            role = info.get('role', 'unknown')
            
            if role == 'slave' or role == 'replica':
                logger.warning(f"⚠️ Connected to Redis replica. Attempting to connect to master...")
                master_host = info.get('master_host')
                master_port = info.get('master_port')
                
                if master_host and master_port:
                    # Close current connection
                    await self.close()
                    
                    # Connect to master
                    master_url = f"redis://{master_host}:{master_port}/0"
                    self._redis_url = master_url
                    
                    self._redis_client = aioredis.from_url(master_url)
                    
                    await self._redis_client.ping()
                    logger.info(f"✅ Connected to Redis master at {master_url}")
            
            self._is_initialized = True
            logger.info(f"✅ Redis connection pool initialized (max_connections=50)")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Redis connection pool: {e}", exc_info=True)
            self._is_initialized = False
            return False
    
    def get_client(self) -> Optional[aioredis.Redis]:
        """
        Get Redis client from pool.
        
        Returns:
            Redis client or None if not initialized
        """
        if not self._is_initialized:
            return None
        return self._redis_client
    
    @asynccontextmanager
    async def get_connection(self):
        """
        Context manager for getting a Redis connection.
        
        Usage:
            async with redis_manager.get_connection() as redis:
                await redis.set("key", "value")
        """
        if not self._is_initialized:
            raise RuntimeError("Redis connection pool not initialized")
        
        try:
            yield self._redis_client
        except Exception as e:
            logger.error(f"❌ Redis operation error: {e}")
            raise
    
    async def ping(self) -> bool:
        """Test Redis connection"""
        if not self._is_initialized or not self._redis_client:
            return False
        
        try:
            await self._redis_client.ping()
            return True
        except Exception as e:
            logger.error(f"❌ Redis ping failed: {e}")
            return False
    
    async def get_info(self) -> dict:
        """Get Redis server info"""
        if not self._is_initialized or not self._redis_client:
            return {}
        
        try:
            info = await self._redis_client.info()
            return info
        except Exception as e:
            logger.error(f"❌ Failed to get Redis info: {e}")
            return {}
    
    async def get_pool_stats(self) -> dict:
        """Get connection pool statistics"""
        if not self._pool:
            return {}
        
        try:
            return {
                "max_connections": self._pool.max_connections,
                "connection_kwargs": {
                    "socket_connect_timeout": self._pool.connection_kwargs.get("socket_connect_timeout"),
                    "socket_timeout": self._pool.connection_kwargs.get("socket_timeout"),
                    "socket_keepalive": self._pool.connection_kwargs.get("socket_keepalive"),
                },
                "redis_url": self._redis_url,
            }
        except Exception as e:
            logger.error(f"❌ Failed to get pool stats: {e}")
            return {}
    
    async def close(self):
        """Close Redis connection pool"""
        if self._redis_client:
            await self._redis_client.close()
            logger.debug("Redis client closed")
        
        if self._pool:
            await self._pool.disconnect()
            logger.debug("Redis connection pool closed")
        
        self._is_initialized = False
        logger.info("Redis connection pool closed")
    
    @property
    def is_available(self) -> bool:
        """Check if Redis is available"""
        return self._is_initialized and self._redis_client is not None


# Global instance
redis_manager = RedisConnectionManager()
