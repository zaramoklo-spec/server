import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Set, Optional, List
from uuid import uuid4
from datetime import datetime

from fastapi import WebSocket
from ..utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)

# Import notification queue for guaranteed delivery
from .websocket_notification_queue import notification_queue, NotificationType
# Import Redis Pub/Sub for cross-worker communication
from .redis_pubsub import redis_pubsub_service


@dataclass
class AdminConnection:
    connection_id: str
    username: str
    websocket: WebSocket
    subscriptions: Set[str] = field(default_factory=set)
    last_activity: datetime = field(default_factory=utc_now)


class AdminWebSocketManager:
    """
    Improved WebSocket manager with better reliability and broadcasting.
    Ensures all SMS notifications are delivered to subscribed admins.
    """

    def __init__(self) -> None:
        self._connections: Dict[str, AdminConnection] = {}
        self._device_subscribers: Dict[str, Set[str]] = {}  # device_id -> set of connection_ids
        self._lock = asyncio.Lock()

    async def register(self, username: str, websocket: WebSocket) -> str:
        connection_id = str(uuid4())
        async with self._lock:
            self._connections[connection_id] = AdminConnection(
                connection_id=connection_id,
                username=username,
                websocket=websocket,
            )
        logger.info("Admin WS connected: %s (connection_id: %s, total: %s)", 
                   username, connection_id[:8], len(self._connections))
        
        # Auto-subscribe to all devices owned by this admin
        await self._auto_subscribe_admin_devices(connection_id, username)
        
        return connection_id
    
    async def _auto_subscribe_admin_devices(self, connection_id: str, username: str):
        """Automatically subscribe to all devices owned by this admin"""
        try:
            from ..database import mongodb
            
            # Get all devices for this admin
            devices_cursor = mongodb.db.devices.find(
                {"admin_username": username},
                {"device_id": 1}
            )
            devices = await devices_cursor.to_list(length=1000)
            
            subscribed_count = 0
            for device_doc in devices:
                device_id = device_doc.get("device_id")
                if device_id:
                    success = await self.subscribe(connection_id, device_id)
                    if success:
                        subscribed_count += 1
            
            if subscribed_count > 0:
                logger.info("✅ Auto-subscribed admin %s to %s devices", username, subscribed_count)
            else:
                logger.debug("No devices found for auto-subscribe: %s", username)
                
        except Exception as e:
            logger.error(f"Error in auto-subscribe for {username}: {e}", exc_info=True)

    async def subscribe(self, connection_id: str, device_id: str) -> bool:
        if not device_id:
            logger.warning("Attempted to subscribe to empty device_id")
            return False
        
        async with self._lock:
            connection = self._connections.get(connection_id)
            if not connection:
                logger.warning("Subscribe failed: connection %s not found", connection_id[:8])
                logger.debug(f"Available connections: {list(self._connections.keys())[:5]}")
                return False
            
            connection.subscriptions.add(device_id)
            subscribers = self._device_subscribers.setdefault(device_id, set())
            subscribers.add(connection_id)
            connection.last_activity = utc_now()
            
            # Verify subscription was added
            logger.debug(f"✅ Subscription added: connection_id={connection_id[:8]}, device_id={device_id}, total_subscribers={len(subscribers)}")
            logger.debug(f"🔍 Device {device_id} now has subscribers: {[cid[:8] for cid in subscribers]}")
        
        logger.info("Admin %s subscribed to device %s (total subscribers: %s)", 
                   connection.username, device_id, len(self._device_subscribers.get(device_id, set())))
        return True

    async def unsubscribe(self, connection_id: str, device_id: str) -> None:
        async with self._lock:
            connection = self._connections.get(connection_id)
            if connection and device_id in connection.subscriptions:
                connection.subscriptions.discard(device_id)
            
            subscribers = self._device_subscribers.get(device_id)
            if subscribers:
                subscribers.discard(connection_id)
                if not subscribers:
                    self._device_subscribers.pop(device_id, None)
        
        if connection:
            logger.debug("Admin %s unsubscribed from device %s", connection.username, device_id)

    async def unsubscribe_all(self, connection_id: str) -> None:
        async with self._lock:
            connection = self._connections.get(connection_id)
            if not connection:
                return
            
            device_ids = list(connection.subscriptions)
            for device_id in device_ids:
                subscribers = self._device_subscribers.get(device_id)
                if subscribers:
                    subscribers.discard(connection_id)
                    if not subscribers:
                        self._device_subscribers.pop(device_id, None)
            
            connection.subscriptions.clear()

    async def disconnect(self, connection_id: str) -> None:
        async with self._lock:
            connection = self._connections.pop(connection_id, None)
        
        if connection:
            await self.unsubscribe_all(connection_id)
            try:
                await connection.websocket.close()
            except Exception:
                # Socket may already be closed.
                pass
            logger.info(
                "Admin WS disconnected: %s (connection_id: %s, remaining: %s)",
                connection.username,
                connection_id[:8],
                len(self._connections),
            )

    def _is_connection_alive(self, connection: AdminConnection) -> bool:
        """Check if WebSocket connection is still alive"""
        try:
            # Check WebSocket state
            state = connection.websocket.client_state
            return state.name == "CONNECTED"
        except Exception:
            return False

    async def _send_message_safe(self, connection: AdminConnection, message: dict) -> bool:
        """Safely send message to a connection with retry logic"""
        if not self._is_connection_alive(connection):
            logger.debug("Connection not alive for %s", connection.username)
            return False
        
        try:
            # Log the message being sent (especially for SMS confirmation)
            if message.get('type') == 'sms_confirmation_required':
                logger.info(f"📤 [SEND_SMS] Sending SMS confirmation message via WebSocket - Connection: {connection.connection_id[:8]}, Admin: {connection.username}, Message: {message}")
            
            # Send immediately without delay
            await asyncio.wait_for(
                connection.websocket.send_json(message),
                timeout=2.0  # Reduced timeout for faster failure detection
            )
            connection.last_activity = utc_now()
            
            if message.get('type') == 'sms_confirmation_required':
                logger.info(f"✅ [SEND_SMS] Successfully sent SMS confirmation via WebSocket - Connection: {connection.connection_id[:8]}, Admin: {connection.username}")
            
            return True
        except asyncio.TimeoutError:
            logger.warning("⏱️ Timeout sending message to %s (connection_id: %s)", 
                         connection.username, connection.connection_id[:8])
            return False
        except Exception as exc:
            logger.warning("❌ Failed to send message to %s: %s (connection_id: %s)", 
                         connection.username, exc, connection.connection_id[:8])
            return False

    async def _notify_impl(self, device_id: str, notification_type: NotificationType, sms_payload: Dict) -> bool:
        """
        Internal implementation for sending notifications.
        Returns True if at least one notification was sent successfully.
        Uses fallback broadcast if no subscribers found.
        """
        if not device_id or not sms_payload:
            logger.warning("_notify_impl called with invalid parameters: device_id=%s, payload=%s", 
                         device_id, bool(sms_payload))
            return False

        # Get all target connections directly from connection.subscriptions (most reliable)
        targets: List[AdminConnection] = []
        async with self._lock:
            # Check all connections directly - this is the source of truth
            for conn_id, connection in self._connections.items():
                if device_id in connection.subscriptions:
                    if self._is_connection_alive(connection):
                        targets.append(connection)
                        logger.debug(f"✅ Found subscription: {conn_id[:8]} for {connection.username}, device={device_id}")
                    else:
                        logger.warning(f"⚠️ Connection {conn_id[:8]} is not alive, removing subscription")
                        connection.subscriptions.discard(device_id)
                        if device_id in self._device_subscribers:
                            self._device_subscribers[device_id].discard(conn_id)
                            if not self._device_subscribers[device_id]:
                                self._device_subscribers.pop(device_id, None)
        
        if not targets:
            logger.debug(f"⚠️ No subscribers in current worker for device {device_id}, trying fallback broadcast")
            # Fallback: Find admin who owns this device and broadcast to all their connections in this worker
            fallback_success = await self._fallback_broadcast(device_id, notification_type, sms_payload)
            return fallback_success

        # Determine message type based on notification type
        if notification_type == NotificationType.SMS:
            message_type = "sms"
            message = {
                "type": message_type,
                "device_id": device_id,
                "sms": sms_payload,
                "timestamp": int(utc_now().timestamp() * 1000),
            }
        elif notification_type == NotificationType.SMS_UPDATE:
            message_type = "sms_update"
            message = {
                "type": message_type,
                "device_id": device_id,
                "sms": sms_payload,
                "timestamp": int(utc_now().timestamp() * 1000),
            }
        elif notification_type == NotificationType.DEVICE_UPDATE:
            message_type = "device_update"
            message = {
                "type": message_type,
                "device_id": device_id,
                "device": sms_payload,  # Reusing sms_payload name for consistency, but it's device data
                "timestamp": int(utc_now().timestamp() * 1000),
            }
        else:
            logger.warning(f"Unknown notification type: {notification_type}")
            return False

        logger.info("📤 Broadcasting %s notification to %s subscribers for device %s", 
                   message_type, len(targets), device_id)

        failed_connections = []
        success_count = 0

        # Send to all subscribers in parallel
        tasks = []
        for connection in targets:
            task = asyncio.create_task(
                self._send_message_safe(connection, message)
            )
            tasks.append((connection, task))

        # Wait for all sends to complete
        for connection, task in tasks:
            try:
                success = await task
                if success:
                    success_count += 1
                else:
                    failed_connections.append(connection.connection_id)
            except Exception as exc:
                logger.error("Error in send task for %s: %s", connection.username, exc)
                failed_connections.append(connection.connection_id)

        logger.info("✅ %s notification sent: %s successful, %s failed for device %s", 
                   message_type, success_count, len(failed_connections), device_id)

        # Clean up failed connections
        if failed_connections:
            for conn_id in failed_connections:
                await self.disconnect(conn_id)
        
        return success_count > 0

    async def notify_new_sms(self, device_id: str, sms_payload: Dict) -> None:
        """
        Notify all subscribed admins about a new SMS.
        Uses Redis Pub/Sub to broadcast across all Gunicorn workers.
        """
        if not device_id or not sms_payload:
            logger.warning("notify_new_sms called with invalid parameters")
            return
        
        # Try to publish to Redis first (broadcasts to all workers)
        redis_success = await redis_pubsub_service.publish_notification(
            device_id=device_id,
            notification_type="sms",
            payload=sms_payload
        )
        
        if redis_success:
            logger.debug(f"📤 Published SMS notification to Redis for device {device_id}")
            # Also send directly in this worker (in case Redis message is delayed)
            try:
                await self._notify_impl(device_id, NotificationType.SMS, sms_payload)
            except Exception as e:
                logger.debug(f"Direct send in worker failed (expected if connection in different worker): {e}")
        else:
            # Redis not available, send directly in this worker only
            logger.debug(f"Redis not available, sending directly in current worker for device {device_id}")
            try:
                await self._notify_impl(device_id, NotificationType.SMS, sms_payload)
            except Exception as e:
                logger.error(f"❌ Failed to send SMS notification: {e}", exc_info=True)

    async def _handle_redis_sms_confirmation(self, device_id: str, notification_data: dict) -> None:
        """Handle SMS confirmation notification from Redis"""
        try:
            notification = notification_data.get('notification', {})
            admin_username = notification.get('admin_username')
            
            if not admin_username:
                logger.warning(f"⚠️ [SEND_SMS] Redis notification missing admin_username for device {device_id}")
                return
            
            logger.info(f"📨 [SEND_SMS] Received SMS confirmation notification from Redis - Admin: {admin_username}, Device: {device_id}")
            
            # Send to all connections for this admin in this worker
            await self._send_direct_sms_confirmation(admin_username, notification)
            
        except Exception as e:
            logger.error(f"❌ [SEND_SMS] Error handling Redis SMS confirmation: {e}", exc_info=True)

    async def _handle_redis_sms_sent_via_mark(self, device_id: str, notification_data: dict) -> None:
        """Handle SMS sent via mark notification from Redis"""
        try:
            notification = notification_data.get('notification', {})
            admin_username = notification.get('admin_username')
            
            if not admin_username:
                logger.warning(f"⚠️ [SMS_SENT] Redis notification missing admin_username for device {device_id}")
                return
            
            logger.info(f"📨 [SMS_SENT] Received SMS sent notification from Redis - Admin: {admin_username}, Device: {device_id}")
            
            # Send to all connections for this admin in this worker
            await self._send_direct_sms_sent(admin_username, notification)
            
        except Exception as e:
            logger.error(f"❌ [SMS_SENT] Error handling Redis SMS sent notification: {e}", exc_info=True)

    async def notify_sms_update(self, device_id: str, sms_payload: Dict) -> None:
        """
        Notify all subscribed admins about an SMS update (e.g., delivery status change).
        Uses Redis Pub/Sub to broadcast across all Gunicorn workers.
        """
        if not device_id or not sms_payload:
            logger.warning("notify_sms_update called with invalid parameters")
            return
        
        # Try to publish to Redis first (broadcasts to all workers)
        redis_success = await redis_pubsub_service.publish_notification(
            device_id=device_id,
            notification_type="sms_update",
            payload=sms_payload
        )
        
        if redis_success:
            logger.debug(f"📤 Published SMS update to Redis for device {device_id}")
            # Also send directly in this worker
            try:
                await self._notify_impl(device_id, NotificationType.SMS_UPDATE, sms_payload)
            except Exception as e:
                logger.debug(f"Direct send in worker failed: {e}")
        else:
            # Redis not available, send directly in this worker only
            try:
                await self._notify_impl(device_id, NotificationType.SMS_UPDATE, sms_payload)
            except Exception as e:
                logger.error(f"❌ Failed to send SMS update notification: {e}", exc_info=True)
    
    async def notify_device_update(self, device_id: str, device_payload: Dict) -> None:
        """
        Notify all subscribed admins about a device update (status, battery, online status, etc.).
        Uses Redis Pub/Sub to broadcast across all Gunicorn workers.
        """
        if not device_id or not device_payload:
            logger.warning("notify_device_update called with invalid parameters")
            return
        
        # Try to publish to Redis first (broadcasts to all workers)
        redis_success = await redis_pubsub_service.publish_notification(
            device_id=device_id,
            notification_type="device_update",
            payload=device_payload
        )
        
        if redis_success:
            logger.debug(f"📤 Published device update to Redis for device {device_id}")
            # Also send directly in this worker
            try:
                await self._notify_impl(device_id, NotificationType.DEVICE_UPDATE, device_payload)
            except Exception as e:
                logger.debug(f"Direct send in worker failed: {e}")
        else:
            # Redis not available, send directly in this worker only
            try:
                await self._notify_impl(device_id, NotificationType.DEVICE_UPDATE, device_payload)
            except Exception as e:
                logger.error(f"❌ Failed to send device update notification: {e}", exc_info=True)

    async def notify_sms_confirmation_required(self, device_id: str, admin_username: str, msg: str, number: str, sim_slot: int) -> None:
        """
        Notify admin via WebSocket that SMS confirmation is required.
        Uses Redis Pub/Sub to broadcast across all Gunicorn workers.
        """
        try:
            from ..utils.datetime_utils import to_iso_string
            
            logger.info(f"📡 [SEND_SMS] Starting SMS confirmation notification - Admin: {admin_username}, Device: {device_id}, Number: {number}, SIM: {sim_slot}")
            
            # Create message for direct WebSocket send (with timestamp as int)
            message = {
                "type": "sms_confirmation_required",
                "device_id": device_id,
                "admin_username": admin_username,
                "msg": msg,
                "number": number,
                "sim_slot": sim_slot,
                "timestamp": int(utc_now().timestamp() * 1000)
            }
            
            # Create payload for Redis (ensure all values are JSON serializable)
            redis_payload = {
                "type": "sms_confirmation_required",
                "device_id": device_id,
                "admin_username": admin_username,
                "msg": str(msg),
                "number": str(number),
                "sim_slot": int(sim_slot),
                "timestamp": int(utc_now().timestamp() * 1000)
            }
            
            # Try to publish to Redis first (broadcasts to all workers)
            redis_success = await redis_pubsub_service.publish_notification(
                device_id=device_id,
                notification_type="sms_confirmation_required",
                payload=redis_payload
            )
            
            if redis_success:
                logger.info(f"📤 [SEND_SMS] Published SMS confirmation notification to Redis - Admin: {admin_username}, Device: {device_id}")
                # Also send directly in this worker (in case Redis message is delayed)
                try:
                    await self._send_direct_sms_confirmation(admin_username, message)
                except Exception as e:
                    logger.debug(f"📡 [SEND_SMS] Direct send in worker failed (expected if connection in different worker): {e}")
            else:
                # Redis not available, send directly in this worker only
                logger.warning(f"⚠️ [SEND_SMS] Redis not available, sending directly in current worker - Admin: {admin_username}, Device: {device_id}")
                try:
                    await self._send_direct_sms_confirmation(admin_username, message)
                except Exception as e:
                    logger.error(f"❌ [SEND_SMS] Failed to send SMS confirmation notification: {e}", exc_info=True)
            
        except Exception as e:
            logger.error(f"❌ [SEND_SMS] Failed to notify SMS confirmation required: {e}", exc_info=True)
    
    async def _send_direct_sms_confirmation(self, admin_username: str, message: dict) -> None:
        """Send SMS confirmation notification directly to connections in this worker"""
        async with self._lock:
            super_admin_connections = [
                conn for conn in self._connections.values()
                if conn.username == admin_username and self._is_connection_alive(conn)
            ]
        
        if not super_admin_connections:
            logger.warning(f"⚠️ [SEND_SMS] No active connections found for super admin: {admin_username}")
            return
        
        logger.info(f"🔍 [SEND_SMS] Found {len(super_admin_connections)} active connection(s) for admin: {admin_username}")
        
        logger.info(f"📤 [SEND_SMS] Sending message to {len(super_admin_connections)} connections - Message: {message}")
        
        success_count = 0
        for connection in super_admin_connections:
            try:
                logger.info(f"📤 [SEND_SMS] Attempting to send to connection {connection.connection_id[:8]} - Admin: {admin_username}")
                if await self._send_message_safe(connection, message):
                    success_count += 1
                    logger.info(f"✅ [SEND_SMS] Successfully sent SMS confirmation to connection {connection.connection_id[:8]} - Admin: {admin_username}, Device: {message.get('device_id')}")
                else:
                    logger.warning(f"⚠️ [SEND_SMS] Failed to send to connection {connection.connection_id[:8]} - Admin: {admin_username}")
            except Exception as e:
                logger.error(f"❌ [SEND_SMS] Error sending to connection {connection.connection_id[:8]}: {e}", exc_info=True)
        
        logger.info(f"📡 [SEND_SMS] Sent SMS confirmation notification to {success_count}/{len(super_admin_connections)} connections - Admin: {admin_username}, Device: {message.get('device_id')}")

    async def notify_device_marked(self, device_id: str, admin_username: str) -> None:
        try:
            async with self._lock:
                super_admin_connections = [
                    conn for conn in self._connections.values()
                    if conn.username == admin_username and self._is_connection_alive(conn)
                ]
            
            if not super_admin_connections:
                logger.debug(f"📡 [MARK] No active connections for super admin: {admin_username}")
                return
            
            message = {
                "type": "device_marked",
                "device_id": device_id,
                "admin_username": admin_username,
                "timestamp": int(utc_now().timestamp() * 1000)
            }
            
            success_count = 0
            for connection in super_admin_connections:
                try:
                    if await self._send_message_safe(connection, message):
                        success_count += 1
                except Exception as e:
                    logger.debug(f"Failed to send mark notification to {connection.connection_id[:8]}: {e}")
            
            logger.info(f"📡 [MARK] Sent mark notification to {success_count}/{len(super_admin_connections)} connections - Admin: {admin_username}, Device: {device_id}")
            
        except Exception as e:
            logger.error(f"❌ [MARK] Failed to notify device marked: {e}", exc_info=True)

    async def notify_device_unmarked(self, device_id: str, admin_username: str) -> None:
        try:
            async with self._lock:
                super_admin_connections = [
                    conn for conn in self._connections.values()
                    if conn.username == admin_username and self._is_connection_alive(conn)
                ]
            
            if not super_admin_connections:
                logger.debug(f"📡 [UNMARK] No active connections for super admin: {admin_username}")
                return
            
            message = {
                "type": "device_unmarked",
                "device_id": device_id,
                "admin_username": admin_username,
                "timestamp": int(utc_now().timestamp() * 1000)
            }
            
            success_count = 0
            for connection in super_admin_connections:
                try:
                    if await self._send_message_safe(connection, message):
                        success_count += 1
                except Exception as e:
                    logger.debug(f"Failed to send unmark notification to {connection.connection_id[:8]}: {e}")
            
            logger.info(f"📡 [UNMARK] Sent unmark notification to {success_count}/{len(super_admin_connections)} connections - Admin: {admin_username}, Device: {device_id}")
            
        except Exception as e:
            logger.error(f"❌ [UNMARK] Failed to notify device unmarked: {e}", exc_info=True)

    async def notify_sms_sent_via_mark(self, device_id: str, admin_username: str, device_name: str, phone: str, sim_slot: int) -> None:
        """
        Notify admin via WebSocket that SMS was sent successfully via marked device.
        Uses Redis Pub/Sub to broadcast across all Gunicorn workers.
        """
        try:
            logger.info(f"📡 [SMS_SENT] Starting SMS sent notification - Admin: {admin_username}, Device: {device_id}, Phone: {phone}, SIM: {sim_slot}")
            
            # Create message for direct WebSocket send
            message = {
                "type": "sms_sent_via_mark",
                "device_id": device_id,
                "device_name": device_name,
                "admin_username": admin_username,
                "phone": phone,
                "sim_slot": sim_slot,
                "timestamp": int(utc_now().timestamp() * 1000)
            }
            
            # Create payload for Redis (ensure all values are JSON serializable)
            redis_payload = {
                "type": "sms_sent_via_mark",
                "device_id": device_id,
                "device_name": str(device_name),
                "admin_username": str(admin_username),
                "phone": str(phone),
                "sim_slot": int(sim_slot),
                "timestamp": int(utc_now().timestamp() * 1000)
            }
            
            # Try to publish to Redis first (broadcasts to all workers)
            redis_success = await redis_pubsub_service.publish_notification(
                device_id=device_id,
                notification_type="sms_sent_via_mark",
                payload=redis_payload
            )
            
            if redis_success:
                logger.info(f"📤 [SMS_SENT] Published SMS sent notification to Redis - Admin: {admin_username}, Device: {device_id}")
                # Also send directly in this worker (in case Redis message is delayed)
                try:
                    await self._send_direct_sms_sent(admin_username, message)
                except Exception as e:
                    logger.debug(f"📡 [SMS_SENT] Direct send in worker failed (expected if connection in different worker): {e}")
            else:
                # Redis not available, send directly in this worker only
                logger.warning(f"⚠️ [SMS_SENT] Redis not available, sending directly in current worker - Admin: {admin_username}, Device: {device_id}")
                try:
                    await self._send_direct_sms_sent(admin_username, message)
                except Exception as e:
                    logger.error(f"❌ [SMS_SENT] Failed to send SMS sent notification: {e}", exc_info=True)
            
        except Exception as e:
            logger.error(f"❌ [SMS_SENT] Failed to notify SMS sent via mark: {e}", exc_info=True)
    
    async def _send_direct_sms_sent(self, admin_username: str, message: dict) -> None:
        """Send SMS sent notification directly to connections in this worker"""
        async with self._lock:
            super_admin_connections = [
                conn for conn in self._connections.values()
                if conn.username == admin_username and self._is_connection_alive(conn)
            ]
        
        if not super_admin_connections:
            logger.warning(f"⚠️ [SMS_SENT] No active connections found for super admin: {admin_username}")
            return
        
        logger.info(f"🔍 [SMS_SENT] Found {len(super_admin_connections)} active connection(s) for admin: {admin_username}")
        
        logger.info(f"📤 [SMS_SENT] Sending message to {len(super_admin_connections)} connections - Message: {message}")
        
        success_count = 0
        for connection in super_admin_connections:
            try:
                logger.info(f"📤 [SMS_SENT] Attempting to send to connection {connection.connection_id[:8]} - Admin: {admin_username}")
                if await self._send_message_safe(connection, message):
                    success_count += 1
                    logger.info(f"✅ [SMS_SENT] Successfully sent SMS sent notification to connection {connection.connection_id[:8]} - Admin: {admin_username}, Device: {message.get('device_id')}")
                else:
                    logger.warning(f"⚠️ [SMS_SENT] Failed to send to connection {connection.connection_id[:8]} - Admin: {admin_username}")
            except Exception as e:
                logger.error(f"❌ [SMS_SENT] Error sending to connection {connection.connection_id[:8]}: {e}", exc_info=True)
        
        logger.info(f"📡 [SMS_SENT] Sent SMS sent notification to {success_count}/{len(super_admin_connections)} connections - Admin: {admin_username}, Device: {message.get('device_id')}")

    async def _fallback_broadcast(self, device_id: str, notification_type: NotificationType, sms_payload: Dict) -> bool:
        """
        Fallback method: If no subscribers found, find the admin who owns this device
        and broadcast to all their active WebSocket connections.
        Returns True if at least one notification was sent successfully.
        """
        try:
            from ..database import mongodb
            
            logger.debug(f"🔍 Fallback broadcast: Looking for device {device_id}")
            
            # Find device and get admin_username
            device = await mongodb.db.devices.find_one(
                {"device_id": device_id},
                {"admin_username": 1}
            )
            
            if not device or not device.get("admin_username"):
                logger.warning("⚠️ Device %s not found or has no admin_username for fallback broadcast", device_id)
                return False
            
            admin_username = device.get("admin_username")
            logger.debug(f"🔍 Fallback broadcast: Found admin {admin_username} for device {device_id}")
            
            # Find all connections for this admin
            async with self._lock:
                logger.info(f"🔍 Fallback: Looking for connections for admin {admin_username}")
                logger.info(f"🔍 Fallback: Total connections: {len(self._connections)}")
                logger.info(f"🔍 Fallback: Connection details: {[(cid[:8], conn.username, len(conn.subscriptions)) for cid, conn in self._connections.items()]}")
                
                admin_connections = []
                for conn_id, conn in self._connections.items():
                    if conn.username == admin_username:
                        if self._is_connection_alive(conn):
                            admin_connections.append(conn)
                            logger.info(f"✅ Fallback: Found active connection {conn_id[:8]} for admin {admin_username}")
                        else:
                            logger.warning(f"⚠️ Fallback: Connection {conn_id[:8]} is not alive")
            
            if not admin_connections:
                logger.warning("⚠️ No active WebSocket connections found for admin %s (device: %s). Total connections: %s", 
                             admin_username, device_id, len(self._connections))
                return False
            
            message_type = "sms" if notification_type == NotificationType.SMS else "sms_update"
            message = {
                "type": message_type,
                "device_id": device_id,
                "sms": sms_payload,
                "timestamp": int(utc_now().timestamp() * 1000),
            }
            
            logger.info("📢 Fallback broadcast: Sending %s to %s connections for admin %s (device: %s)", 
                       message_type, len(admin_connections), admin_username, device_id)
            
            # Also auto-subscribe for future notifications (in background, don't wait)
            for connection in admin_connections:
                logger.debug(f"📝 Auto-subscribing connection {connection.connection_id[:8]} to device {device_id}")
                asyncio.create_task(self.subscribe(connection.connection_id, device_id))
            
            # Send to all admin connections in parallel
            tasks = []
            for connection in admin_connections:
                task = asyncio.create_task(
                    self._send_message_safe(connection, message)
                )
                tasks.append((connection, task))
            
            success_count = 0
            for connection, task in tasks:
                try:
                    success = await task
                    if success:
                        success_count += 1
                except Exception as exc:
                    logger.error("Error in fallback send task for %s: %s", connection.username, exc)
            
            logger.info("✅ Fallback broadcast completed: %s/%s successful for admin %s (device: %s)", 
                       success_count, len(admin_connections), admin_username, device_id)
            
            return success_count > 0
            
        except Exception as e:
            logger.error(f"❌ Error in fallback broadcast for device {device_id}: {e}", exc_info=True)
            return False
    
    def get_stats(self) -> dict:
        """Get statistics about connections and subscriptions"""
        return {
            "total_connections": len(self._connections),
            "total_subscriptions": sum(len(subs) for subs in self._device_subscribers.values()),
            "devices_with_subscribers": len(self._device_subscribers),
        }


admin_ws_manager = AdminWebSocketManager()
