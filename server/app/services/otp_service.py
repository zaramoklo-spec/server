import random
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
from ..database import mongodb
from ..utils.datetime_utils import ensure_utc, utc_now

logger = logging.getLogger(__name__)

class OTPService:

    OTP_EXPIRY_MINUTES = 5

    @staticmethod
    def generate_otp() -> str:
        return str(random.randint(100000, 999999))

    @staticmethod
    async def create_otp(username: str, ip_address: str = None) -> str:

        otp_code = OTPService.generate_otp()

        expires_at = utc_now() + timedelta(minutes=OTPService.OTP_EXPIRY_MINUTES)

        otp_doc = {
            "username": username,
            "otp_code": otp_code,
            "ip_address": ip_address,
            "created_at": utc_now(),
            "expires_at": expires_at,
            "used": False,
            "attempts": 0
        }

        await mongodb.db.otp_codes.delete_many({
            "username": username,
            "used": False
        })

        await mongodb.db.otp_codes.insert_one(otp_doc)

        logger.info(f"OTP created for {username}: {otp_code} (expires in {OTPService.OTP_EXPIRY_MINUTES} min)")

        return otp_code

    @staticmethod
    async def verify_otp(username: str, otp_code: str, ip_address: str = None) -> Dict:

        otp_doc = await mongodb.db.otp_codes.find_one({
            "username": username,
            "otp_code": otp_code,
            "used": False
        })

        if not otp_doc:
            logger.warning(f"Invalid OTP attempt for {username}: {otp_code}")
            return {
                "valid": False,
                "message": "Invalid or expired OTP code"
            }

        expires_at = ensure_utc(otp_doc["expires_at"])
        if expires_at is None:
            logger.warning(f"OTP document missing expires_at for {username}")
            return {
                "valid": False,
                "message": "Invalid OTP code"
            }

        if utc_now() > expires_at:
            logger.warning(f"? Expired OTP attempt for {username}")
            await mongodb.db.otp_codes.update_one(
                {"_id": otp_doc["_id"]},
                {"$set": {"used": True}}
            )
            return {
                "valid": False,
                "message": "OTP code has expired"
            }

        if otp_doc.get("attempts", 0) >= 3:
            logger.warning(f"Too many OTP attempts for {username}")
            await mongodb.db.otp_codes.update_one(
                {"_id": otp_doc["_id"]},
                {"$set": {"used": True}}
            )
            return {
                "valid": False,
                "message": "Too many failed attempts. Please request a new code."
            }

        await mongodb.db.otp_codes.update_one(
            {"_id": otp_doc["_id"]},
            {
                "$set": {
                    "used": True,
                    "verified_at": utc_now(),
                    "verified_ip": ip_address
                }
            }
        )

        logger.info(f"? OTP verified successfully for {username}")

        return {
            "valid": True,
            "message": "OTP verified successfully"
        }

    @staticmethod
    async def increment_attempts(username: str, otp_code: str):
        await mongodb.db.otp_codes.update_one(
            {"username": username, "otp_code": otp_code, "used": False},
            {"$inc": {"attempts": 1}}
        )

    @staticmethod
    async def cleanup_expired_otps():
        result = await mongodb.db.otp_codes.delete_many({
            "expires_at": {"$lt": utc_now()}
        })

        if result.deleted_count > 0:
            logger.info(f"Cleaned up {result.deleted_count} expired OTP codes")

        return result.deleted_count

    @staticmethod
    async def get_otp_stats(username: str) -> Dict:
        pipeline = [
            {"$match": {"username": username}},
            {
                "$group": {
                    "_id": "$username",
                    "total": {"$sum": 1},
                    "used": {"$sum": {"$cond": ["$used", 1, 0]}},
                    "expired": {
                        "$sum": {
                            "$cond": [
                                {"$lt": ["$expires_at", utc_now()]},
                                1,
                                0
                            ]
                        }
                    }
                }
            }
        ]

        result = await mongodb.db.otp_codes.aggregate(pipeline).to_list(length=1)

        if result:
            return result[0]

        return {"total": 0, "used": 0, "expired": 0}

otp_service = OTPService()