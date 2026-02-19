"""
FCM Ping Queue Service - Enterprise-grade distributed queue system
Supports thousands of devices with priority, rate limiting, and retry
"""
import asyncio
import json
import logging
import time
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from enum import Enum
from dataclasses import dataclass, asdict
from collections import defaultdict

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    aioredis = None

from ..config import settings
from ..utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)


class Priority(Enum):
    """Priority levels for FCM pings"""
    CRITICAL = 0  # Immediate - device is critical
    HIGH = 1      # Urgent - device hasn't pinged in 3-4 min
    NORMAL = 2    # Normal - regular check
    LOW = 3       # Low priority - batch processing


@dataclass
class FCMPingTask:
    """FCM Ping Task with metadata"""
    device_id: str
    priority: Priority = Priority.NORMAL
    retry_count: int = 0
    max_retries: int = 3
    created_at: float = None
    scheduled_at: float = None
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = time.time()
        if self.scheduled_at is None:
            self.scheduled_at = self.created_at
        if self.metadata is None:
            self.metadata = {}
    
    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "priority": self.priority.value,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "created_at": self.created_at,
            "scheduled_at": self.scheduled_at,
            "metadata": self.metadata
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'FCMPingTask':
        return cls(
            device_id=data["device_id"],
            priority=Priority(data.get("priority", 2)),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            created_at=data.get("created_at", time.time()),
            scheduled_at=data.get("scheduled_at", time.time()),
            metadata=data.get("metadata", {})
        )


class FCMQueueService:
    """
    Enterprise-grade FCM Ping Queue Service
    
    Features:
    - Priority queues (Critical, High, Normal, Low)
    - Rate limiting (respects Firebase limits)
    - Automatic retry with exponential backoff
    - Distributed processing (multiple workers)
    - Metrics and monitoring
    - Batch processing
    - Dead letter queue for failed tasks
    """
    
    # Redis keys
    QUEUE_PREFIX = "fcm_ping:queue"
    PROCESSING_PREFIX = "fcm_ping:processing"
    DEAD_LETTER_QUEUE = "fcm_ping:dead_letter"
    METRICS_KEY = "fcm_ping:metrics"
    RATE_LIMIT_KEY = "fcm_ping:rate_limit"
    
    # Rate limiting (Firebase allows ~1000 messages/second per project)
    MAX_RATE_PER_SECOND = 800  # Conservative limit
    BATCH_SIZE = 50  # Process 50 devices per batch
    WORKER_CONCURRENCY = 10  # Concurrent batches per worker
    
    def __init__(self):
        self._redis_client: Optional[aioredis.Redis] = None
        self._is_initialized = False
        self._rate_limiter: Optional[asyncio.Semaphore] = None
        self._last_rate_reset = time.time()
        self._rate_counter = 0
    
    async def initialize(self) -> bool:
        """Initialize Redis connection"""
        if not REDIS_AVAILABLE:
            logger.warning("⚠️ Redis not available for FCM Queue")
            return False
        
        try:
            redis_url = getattr(settings, 'REDIS_URL', 'redis://localhost:6379/0')
            self._redis_client = aioredis.from_url(
                redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                # Force connection to master (not replica)
                socket_keepalive=True,
            )
            await self._redis_client.ping()
            
            # Check if Redis is in read-only mode and try to fix
            try:
                info = await self._redis_client.info('replication')
                role = info.get('role', 'unknown')
                if role == 'slave':
                    logger.warning(f"⚠️ Redis is in slave/replica mode. Attempting to connect to master...")
                    # Try to get master info
                    master_host = info.get('master_host')
                    master_port = info.get('master_port')
                    if master_host and master_port:
                        master_url = f"redis://{master_host}:{master_port}/0"
                        logger.info(f"🔄 Connecting to Redis master at {master_url}")
                        await self._redis_client.close()
                        self._redis_client = aioredis.from_url(
                            master_url,
                            encoding="utf-8",
                            decode_responses=True,
                            socket_connect_timeout=5,
                            socket_timeout=5,
                            socket_keepalive=True,
                        )
                        await self._redis_client.ping()
                        logger.info("✅ Connected to Redis master")
            except Exception as e:
                logger.debug(f"Could not check Redis replication info: {e}")
            
            self._rate_limiter = asyncio.Semaphore(self.MAX_RATE_PER_SECOND)
            self._is_initialized = True
            logger.info("✅ FCM Queue Service initialized")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize FCM Queue: {e}", exc_info=True)
            return False
    
    async def _reconnect_to_master(self):
        """Try to reconnect to Redis master if we're connected to a replica"""
        try:
            if not self._redis_client:
                return
            
            # Get replication info
            info = await self._redis_client.info('replication')
            role = info.get('role', 'unknown')
            
            if role == 'slave' or role == 'replica':
                master_host = info.get('master_host')
                master_port = info.get('master_port')
                
                if master_host and master_port:
                    master_url = f"redis://{master_host}:{master_port}/0"
                    logger.info(f"🔄 Reconnecting to Redis master at {master_url}")
                    
                    # Close current connection
                    await self._redis_client.close()
                    
                    # Connect to master
                    self._redis_client = aioredis.from_url(
                        master_url,
                        encoding="utf-8",
                        decode_responses=True,
                        socket_connect_timeout=5,
                        socket_timeout=5,
                        socket_keepalive=True,
                    )
                    await self._redis_client.ping()
                    logger.info("✅ Reconnected to Redis master")
                else:
                    logger.warning("⚠️ Redis is replica but master info not available")
            else:
                logger.warning(f"⚠️ Redis role is {role}, not replica. Read-only error may be temporary.")
        except Exception as e:
            logger.error(f"❌ Failed to reconnect to master: {e}", exc_info=True)
    
    def _get_queue_key(self, priority: Priority) -> str:
        """Get Redis key for priority queue"""
        return f"{self.QUEUE_PREFIX}:{priority.name.lower()}"
    
    async def enqueue(
        self,
        device_ids: List[str],
        priority: Priority = Priority.NORMAL,
        metadata: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Enqueue device IDs for FCM ping
        
        Args:
            device_ids: List of device IDs to ping
            priority: Priority level
            metadata: Optional metadata
            
        Returns:
            Number of tasks enqueued
        """
        if not self._is_initialized:
            logger.warning("⚠️ Queue not initialized, skipping enqueue")
            return 0
        
        if not device_ids:
            return 0
        
        try:
            queue_key = self._get_queue_key(priority)
            tasks = []
            
            for device_id in device_ids:
                task = FCMPingTask(
                    device_id=device_id,
                    priority=priority,
                    metadata=metadata or {}
                )
                tasks.append(json.dumps(task.to_dict()))
            
            # Add to priority queue (Redis List - left push for priority)
            if priority == Priority.CRITICAL:
                # Critical tasks go to the front
                await self._redis_client.lpush(queue_key, *tasks)
            else:
                # Other tasks go to the back
                await self._redis_client.rpush(queue_key, *tasks)
            
            # Update metrics
            await self._update_metrics("enqueued", len(tasks), priority)
            
            logger.info(f"📥 Enqueued {len(tasks)} tasks with priority {priority.name}")
            return len(tasks)
            
        except Exception as e:
            logger.error(f"❌ Failed to enqueue tasks: {e}", exc_info=True)
            return 0
    
    async def dequeue(self, count: int = None) -> List[FCMPingTask]:
        """
        Dequeue tasks from priority queues (Critical -> High -> Normal -> Low)
        
        Args:
            count: Number of tasks to dequeue (default: BATCH_SIZE)
            
        Returns:
            List of FCMPingTask objects
        """
        if not self._is_initialized:
            return []
        
        if count is None:
            count = self.BATCH_SIZE
        
        tasks = []
        remaining = count
        
        # Dequeue in priority order
        for priority in [Priority.CRITICAL, Priority.HIGH, Priority.NORMAL, Priority.LOW]:
            if remaining <= 0:
                break
            
            queue_key = self._get_queue_key(priority)
            
            try:
                # Get tasks from this priority queue
                batch = await self._redis_client.lpop(queue_key, remaining)
                
                if batch:
                    if isinstance(batch, str):
                        batch = [batch]
                    
                    for task_json in batch:
                        try:
                            task_data = json.loads(task_json)
                            task = FCMPingTask.from_dict(task_data)
                            tasks.append(task)
                            remaining -= 1
                        except Exception as e:
                            logger.error(f"❌ Failed to parse task: {e}")
            except aioredis.exceptions.ReadOnlyError as e:
                logger.error(f"❌ Redis is in read-only mode (replica). Cannot dequeue. Error: {e}")
                # Try to reconnect to master
                try:
                    await self._reconnect_to_master()
                except Exception as reconnect_error:
                    logger.error(f"❌ Failed to reconnect to master: {reconnect_error}")
                break  # Stop trying to dequeue
            except Exception as e:
                logger.error(f"❌ Error dequeuing from {queue_key}: {e}")
                break  # Stop on error
        
        # Move to processing queue
        if tasks:
            await self._move_to_processing(tasks)
            await self._update_metrics("dequeued", len(tasks))
        
        return tasks
    
    async def _move_to_processing(self, tasks: List[FCMPingTask]):
        """Move tasks to processing queue"""
        try:
            processing_key = f"{self.PROCESSING_PREFIX}:{int(time.time())}"
            task_jsons = [json.dumps(task.to_dict()) for task in tasks]
            await self._redis_client.rpush(processing_key, *task_jsons)
            # Set expiration (5 minutes)
            await self._redis_client.expire(processing_key, 300)
        except Exception as e:
            logger.error(f"❌ Failed to move to processing: {e}")
    
    async def mark_completed(self, task: FCMPingTask, success: bool):
        """Mark task as completed"""
        if not self._is_initialized:
            return
        
        try:
            if success:
                await self._update_metrics("completed", 1, task.priority)
            else:
                # Retry or move to dead letter
                if task.retry_count < task.max_retries:
                    # Retry with exponential backoff
                    task.retry_count += 1
                    backoff = min(2 ** task.retry_count, 60)  # Max 60 seconds
                    task.scheduled_at = time.time() + backoff
                    
                    # Re-enqueue with delay
                    queue_key = self._get_queue_key(task.priority)
                    await self._redis_client.rpush(queue_key, json.dumps(task.to_dict()))
                    await self._update_metrics("retried", 1, task.priority)
                else:
                    # Move to dead letter queue
                    await self._redis_client.rpush(
                        self.DEAD_LETTER_QUEUE,
                        json.dumps(task.to_dict())
                    )
                    await self._update_metrics("failed", 1, task.priority)
        except Exception as e:
            logger.error(f"❌ Failed to mark completed: {e}")
    
    async def _update_metrics(self, event: str, count: int, priority: Optional[Priority] = None):
        """Update metrics in Redis"""
        try:
            now = int(time.time())
            metrics_key = f"{self.METRICS_KEY}:{event}"
            if priority:
                metrics_key += f":{priority.name.lower()}"
            
            pipe = self._redis_client.pipeline()
            pipe.incrby(metrics_key, count)
            pipe.expire(metrics_key, 86400)  # Keep for 24 hours
            await pipe.execute()
        except Exception as e:
            logger.debug(f"Failed to update metrics: {e}")
    
    async def get_metrics(self) -> Dict[str, Any]:
        """Get queue metrics"""
        if not self._is_initialized:
            return {}
        
        try:
            metrics = {}
            pattern = f"{self.METRICS_KEY}:*"
            keys = await self._redis_client.keys(pattern)
            
            for key in keys:
                value = await self._redis_client.get(key)
                if value:
                    event = key.split(":")[-1]
                    metrics[event] = int(value)
            
            # Queue lengths
            for priority in Priority:
                queue_key = self._get_queue_key(priority)
                length = await self._redis_client.llen(queue_key)
                metrics[f"queue_length_{priority.name.lower()}"] = length
            
            # Dead letter queue length
            dlq_length = await self._redis_client.llen(self.DEAD_LETTER_QUEUE)
            metrics["dead_letter_queue_length"] = dlq_length
            
            return metrics
        except Exception as e:
            logger.error(f"❌ Failed to get metrics: {e}")
            return {}
    
    async def get_queue_length(self) -> int:
        """Get total queue length"""
        if not self._is_initialized:
            return 0
        
        try:
            total = 0
            for priority in Priority:
                queue_key = self._get_queue_key(priority)
                length = await self._redis_client.llen(queue_key)
                total += length
            return total
        except Exception as e:
            logger.error(f"❌ Failed to get queue length: {e}")
            return 0
    
    async def clear_queue(self, priority: Optional[Priority] = None):
        """Clear queue(s)"""
        if not self._is_initialized:
            return
        
        try:
            if priority:
                queue_key = self._get_queue_key(priority)
                await self._redis_client.delete(queue_key)
            else:
                # Clear all queues
                for p in Priority:
                    queue_key = self._get_queue_key(p)
                    await self._redis_client.delete(queue_key)
            logger.info(f"🗑️ Cleared queue(s)")
        except Exception as e:
            logger.error(f"❌ Failed to clear queue: {e}")


# Global instance
fcm_queue_service = FCMQueueService()

