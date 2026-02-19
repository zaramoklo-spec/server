"""
Redis Pub/Sub Service for WebSocket notifications across Gunicorn workers
"""
import asyncio
import logging
import json
from typing import Optional, Callable, Awaitable, Any
from datetime import datetime
try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    aioredis = None

from ..config import settings

logger = logging.getLogger(__name__)


class RedisPubSubService:
    """
    Redis Pub/Sub service for broadcasting SMS notifications across all Gunicorn workers.
    This solves the worker isolation problem.
    """
    
    def __init__(self):
        self._redis_client: Optional[aioredis.Redis] = None
        self._pubsub: Optional[aioredis.client.PubSub] = None
        self._subscriber_task: Optional[asyncio.Task] = None
        self._is_running = False
        self._notification_handler: Optional[Callable[[str, dict], Awaitable[None]]] = None
        self._channel_name = "sms_notifications"
    
    async def initialize(self):
        """Initialize Redis connection"""
        if not REDIS_AVAILABLE:
            logger.warning("⚠️ Redis library not installed. Install with: pip install redis aioredis")
            return False
        
        try:
            # Try to connect to Redis (optional - if not available, will use direct send)
            redis_url = getattr(settings, 'REDIS_URL', 'redis://localhost:6379/0')
            self._redis_client = aioredis.from_url(
                redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            
            # Test connection
            await self._redis_client.ping()
            logger.info("✅ Redis connected successfully")
            return True
        except Exception as e:
            logger.warning(f"⚠️ Redis not available: {e}. Will use direct WebSocket send.")
            self._redis_client = None
            return False
    
    def set_notification_handler(self, handler: Callable[[str, dict], Awaitable[None]]):
        """Set the handler function that will process notifications"""
        self._notification_handler = handler
    
    async def start_subscriber(self):
        """Start listening to Redis Pub/Sub messages"""
        if not self._redis_client:
            logger.warning("Redis not available, subscriber not started")
            return
        
        if self._is_running:
            return
        
        try:
            self._pubsub = self._redis_client.pubsub()
            await self._pubsub.subscribe(self._channel_name)
            self._is_running = True
            
            # Start subscriber task
            self._subscriber_task = asyncio.create_task(self._subscriber_loop())
            logger.info(f"✅ Redis Pub/Sub subscriber started on channel: {self._channel_name}")
        except Exception as e:
            logger.error(f"❌ Failed to start Redis subscriber: {e}", exc_info=True)
            self._is_running = False
    
    async def _subscriber_loop(self):
        """Main loop for receiving messages from Redis"""
        while self._is_running:
            try:
                message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                
                if message and message['type'] == 'message':
                    try:
                        data = json.loads(message['data'])
                        device_id = data.get('device_id')
                        notification_type = data.get('notification_type')
                        notification_payload = data.get('notification')
                        
                        if device_id and notification_payload and self._notification_handler:
                            # Pass both device_id and full notification data
                            notification_data = {
                                'notification_type': notification_type,
                                'notification': notification_payload
                            }
                            await self._notification_handler(device_id, notification_data)
                            logger.debug(f"📨 Processed Redis notification for device {device_id}, type={notification_type}")
                    except Exception as e:
                        logger.error(f"❌ Error processing Redis message: {e}", exc_info=True)
                        
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"❌ Error in Redis subscriber loop: {e}", exc_info=True)
                await asyncio.sleep(1)
    
    def _serialize_payload(self, payload: Any) -> Any:
        """Recursively serialize payload to ensure all values are JSON serializable"""
        try:
            if payload is None:
                return None
            
            # Check for datetime objects FIRST (before other checks)
            if isinstance(payload, datetime):
                return payload.isoformat()
            
            # Check for bson datetime (from pymongo)
            try:
                from bson import datetime as bson_datetime
                if isinstance(payload, bson_datetime.datetime):
                    return payload.isoformat()
            except (ImportError, AttributeError):
                pass
            
            # Check for ObjectId (from pymongo)
            try:
                from bson import ObjectId
                if isinstance(payload, ObjectId):
                    return str(payload)
            except (ImportError, AttributeError):
                pass
            
            # Check for datetime-like objects (has isoformat method)
            if hasattr(payload, 'isoformat') and callable(getattr(payload, 'isoformat')):
                try:
                    # Check if it's actually a datetime-like object
                    if hasattr(payload, 'year') and hasattr(payload, 'month') and hasattr(payload, 'day'):
                        return payload.isoformat()
                except:
                    pass
            
            # Handle dicts - recursively serialize all values
            if isinstance(payload, dict):
                return {str(key): self._serialize_payload(value) for key, value in payload.items()}
            
            # Handle lists and tuples - recursively serialize all items
            if isinstance(payload, (list, tuple)):
                return [self._serialize_payload(item) for item in payload]
            
            # Handle sets
            if isinstance(payload, set):
                return [self._serialize_payload(item) for item in payload]
            
            # Already JSON serializable types
            if isinstance(payload, (str, int, float, bool)):
                return payload
            
            # Handle objects with __dict__
            if hasattr(payload, '__dict__'):
                return self._serialize_payload(payload.__dict__)
            
            # Fallback: convert to string
            return str(payload)
        except Exception as e:
            logger.error(f"❌ Error serializing payload: {e}, type: {type(payload)}")
            return str(payload)

    def _json_serializer(self, obj):
        """Custom JSON serializer for datetime and other non-serializable types"""
        # Handle datetime objects
        if isinstance(obj, datetime):
            return obj.isoformat()
        
        # Check for bson datetime (from pymongo)
        try:
            from bson import datetime as bson_datetime
            if isinstance(obj, bson_datetime.datetime):
                return obj.isoformat()
        except ImportError:
            pass
        
        # Check for objects with isoformat method (datetime-like)
        if hasattr(obj, 'isoformat') and callable(getattr(obj, 'isoformat')):
            try:
                return obj.isoformat()
            except:
                pass
        
        # Check for other datetime-like objects
        if hasattr(obj, 'year') and hasattr(obj, 'month') and hasattr(obj, 'day'):
            try:
                if hasattr(obj, 'isoformat'):
                    return obj.isoformat()
                # Fallback: create ISO string manually
                return f"{obj.year}-{obj.month:02d}-{obj.day:02d}T{obj.hour:02d}:{obj.minute:02d}:{obj.second:02d}"
            except:
                pass
        
        # For any other non-serializable type, convert to string
        return str(obj)

    async def publish_notification(self, device_id: str, notification_type: str, payload: dict):
        """Publish notification to Redis (all workers will receive it)"""
        if not self._redis_client:
            # Redis not available, return False so caller can use direct send
            return False
        
        try:
            # First, serialize payload recursively to handle all datetime objects
            serialized_payload = self._serialize_payload(payload)
            
            # Create message and serialize it too (in case payload had nested structures)
            message = {
                "device_id": str(device_id),
                "notification_type": str(notification_type),
                "notification": serialized_payload,
                "timestamp": int(asyncio.get_event_loop().time() * 1000)
            }
            
            # Serialize the entire message structure recursively
            serialized_message = self._serialize_payload(message)
            
            # Use json.dumps with default callback as final safety net
            json_str = json.dumps(serialized_message, default=self._json_serializer, ensure_ascii=False)
            
            await self._redis_client.publish(
                self._channel_name,
                json_str
            )
            
            logger.debug(f"📤 Published notification to Redis: device={device_id}, type={notification_type}")
            return True
        except TypeError as e:
            # If TypeError (datetime serialization), try one more time with more aggressive serialization
            try:
                logger.warning(f"⚠️ First serialization failed, retrying with aggressive serialization: {e}")
                # Serialize the entire message again with even more aggressive approach
                serialized_message = self._serialize_payload(message)
                # Try to serialize again in case we missed something
                final_serialized = self._serialize_payload(serialized_message)
                json_str = json.dumps(final_serialized, default=self._json_serializer, ensure_ascii=False)
                await self._redis_client.publish(self._channel_name, json_str)
                logger.debug(f"📤 Published notification to Redis (retry): device={device_id}, type={notification_type}")
                return True
            except Exception as retry_e:
                logger.error(f"❌ Failed to publish to Redis (retry failed): {retry_e}", exc_info=True)
                return False
        except Exception as e:
            logger.error(f"❌ Failed to publish to Redis: {e}", exc_info=True)
            import traceback
            logger.error(f"❌ Traceback: {traceback.format_exc()}")
            return False
    
    async def stop(self):
        """Stop the subscriber"""
        self._is_running = False
        
        if self._subscriber_task:
            self._subscriber_task.cancel()
            try:
                await self._subscriber_task
            except asyncio.CancelledError:
                pass
        
        if self._pubsub:
            await self._pubsub.unsubscribe(self._channel_name)
            await self._pubsub.close()
        
        if self._redis_client:
            await self._redis_client.close()
        
        logger.info("Redis Pub/Sub service stopped")


# Global instance
redis_pubsub_service = RedisPubSubService()

