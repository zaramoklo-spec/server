from datetime import datetime, timedelta, timezone
from typing import Optional, List
import logging
from bson import ObjectId

from ..database import mongodb
from ..models.admin_schemas import AdminActivity, ActivityType
from ..utils.datetime_utils import ensure_utc, to_iso_string, utc_now

logger = logging.getLogger(__name__)

class AdminActivityService:

    @staticmethod
    async def log_activity(
        admin_username: str,
        activity_type: ActivityType,
        description: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        device_id: Optional[str] = None,
        metadata: dict = None,
        success: bool = True,
        error_message: Optional[str] = None,
        send_telegram: bool = True
    ):
        try:
            activity = AdminActivity(
                admin_username=admin_username,
                activity_type=activity_type,
                description=description,
                ip_address=ip_address,
                user_agent=user_agent,
                device_id=device_id,
                metadata=metadata or {},
                success=success,
                error_message=error_message
            )

            await mongodb.db.admin_activities.insert_one(activity.model_dump())

            logger.info(f"Activity logged: {admin_username} - {activity_type.value}")

            if send_telegram:
                try:
                    from .telegram_multi_service import telegram_multi_service

                    if activity_type in [ActivityType.LOGIN, ActivityType.LOGOUT]:
                        if activity_type == ActivityType.LOGIN:
                            await telegram_multi_service.notify_admin_login(
                                admin_username=admin_username,
                                ip_address=ip_address or "unknown",
                                success=success
                            )
                        else:
                            await telegram_multi_service.notify_admin_logout(
                                admin_username=admin_username,
                                ip_address=ip_address or "unknown"
                            )
                    else:
                        details = description
                        if not success and error_message:
                            details += f"\n❌ Error: {error_message}"

                        await telegram_multi_service.notify_admin_action(
                            admin_username=admin_username,
                            action=activity_type.value,
                            details=details,
                            ip_address=ip_address,
                            device_id=device_id
                        )

                    logger.debug(f"Telegram notification sent for activity: {activity_type.value}")

                except Exception as telegram_error:
                    logger.warning(f"Failed to send Telegram notification: {telegram_error}")

        except Exception as e:
            logger.error(f"Failed to log activity: {e}")

    @staticmethod
    async def get_activities(
        admin_username: Optional[str] = None,
        activity_type: Optional[ActivityType] = None,
        device_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        skip: int = 0,
        limit: int = 100
    ) -> List[dict]:
        try:
            query = {}

            if admin_username:
                query["admin_username"] = admin_username

            if activity_type:
                query["activity_type"] = activity_type

            if device_id:
                query["device_id"] = device_id

            if start_date or end_date:
                query["timestamp"] = {}
                if start_date:
                    query["timestamp"]["$gte"] = ensure_utc(start_date) or start_date
                if end_date:
                    query["timestamp"]["$lte"] = ensure_utc(end_date) or end_date

            cursor = mongodb.db.admin_activities.find(query).sort("timestamp", -1).skip(skip).limit(limit)

            activities = await cursor.to_list(length=limit)

            for activity in activities:
                if "_id" in activity:
                    activity["_id"] = str(activity["_id"])
                for key, value in activity.items():
                    if isinstance(value, ObjectId):
                        activity[key] = str(value)
                timestamp = activity.get("timestamp")
                if timestamp:
                    activity["timestamp"] = to_iso_string(ensure_utc(timestamp))

            return activities

        except Exception as e:
            logger.error(f"Failed to get activities: {e}")
            return []

    @staticmethod
    async def get_activity_stats(admin_username: Optional[str] = None) -> dict:
        try:
            pipeline = []

            if admin_username:
                pipeline.append({"$match": {"admin_username": admin_username}})

            pipeline.extend([
                {
                    "$group": {
                        "_id": "$activity_type",
                        "count": {"$sum": 1}
                    }
                }
            ])

            cursor = mongodb.db.admin_activities.aggregate(pipeline)
            results = await cursor.to_list(length=100)

            stats = {item["_id"]: item["count"] for item in results}

            return stats

        except Exception as e:
            logger.error(f"Failed to get activity stats: {e}")
            return {}

    @staticmethod
    async def get_recent_logins(limit: int = 10) -> List[dict]:
        try:
            cursor = mongodb.db.admin_activities.find(
                {"activity_type": ActivityType.LOGIN}
            ).sort("timestamp", -1).limit(limit)

            logins = await cursor.to_list(length=limit)

            for login in logins:
                if "_id" in login:
                    login["_id"] = str(login["_id"])
                timestamp = login.get("timestamp")
                if timestamp:
                    login["timestamp"] = to_iso_string(ensure_utc(timestamp))

            return logins

        except Exception as e:
            logger.error(f"Failed to get recent logins: {e}")
            return []

    @staticmethod
    async def cleanup_old_activities(days: int = 90):
        try:
            cutoff_date = utc_now() - timedelta(days=days)

            result = await mongodb.db.admin_activities.delete_many(
                {"timestamp": {"$lt": cutoff_date}}
            )

            logger.info(f"Deleted {result.deleted_count} old activities")

            return result.deleted_count

        except Exception as e:
            logger.error(f"Failed to cleanup activities: {e}")
            return 0

admin_activity_service = AdminActivityService()