from datetime import datetime, timedelta, timezone
from typing import Optional
from passlib.context import CryptContext
from jose import JWTError, jwt
from fastapi import HTTPException, status
import logging
import secrets

from ..database import mongodb
from ..config import settings
from ..utils.datetime_utils import ensure_utc, utc_now
from ..models.admin_schemas import (
    Admin, AdminCreate, AdminUpdate, AdminLogin,
    AdminResponse, AdminRole, AdminPermission, ROLE_PERMISSIONS, TelegramBot
)

ENABLE_2FA = True

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

class AuthService:

    @staticmethod
    def hash_password(password: str) -> str:
        return pwd_context.hash(password)

    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        return pwd_context.verify(plain_password, hashed_password)

    @staticmethod
    def generate_device_token() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def generate_session_id() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def create_access_token(
        data: dict,
        expires_delta: Optional[timedelta] = None,
        session_id: str = None,
        is_bot: bool = False,
        client_type: str = None
    ) -> str:
        to_encode = data.copy()

        if client_type is None:
            client_type = "service" if is_bot else "interactive"

        to_encode.update({"client_type": client_type})

        if client_type != "service":
            if expires_delta:
                expire = utc_now() + expires_delta
            else:
                expire = utc_now() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
            to_encode.update({"exp": expire})

        if session_id and client_type == "interactive":
            to_encode.update({"session_id": session_id})

        encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)

        return encoded_jwt

    @staticmethod
    def create_temp_token(username: str) -> str:
        data = {
            "sub": username,
            "type": "temp_2fa",
            "exp": utc_now() + timedelta(minutes=5)
        }
        return jwt.encode(data, settings.SECRET_KEY, algorithm=ALGORITHM)

    @staticmethod
    def verify_temp_token(token: str) -> Optional[str]:
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
            if payload.get("type") != "temp_2fa":
                return None
            return payload.get("sub")
        except JWTError:
            return None

    @staticmethod
    async def verify_token(token: str) -> dict:
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
            username: str = payload.get("sub")

            if username is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid authentication credentials",
                )

            return payload
        except JWTError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
            )

    @staticmethod
    async def create_admin(admin_create: AdminCreate, created_by: str = None) -> Admin:
        try:

            existing = await mongodb.db.admins.find_one({"username": admin_create.username})
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Username already exists"
                )

            password_hash = AuthService.hash_password(admin_create.password)

            if not admin_create.permissions:
                admin_create.permissions = ROLE_PERMISSIONS[admin_create.role]

            device_token = AuthService.generate_device_token()

            telegram_bots = admin_create.telegram_bots or []

            if len(telegram_bots) == 0:

                logger.info(f"Creating 5 placeholder bots for {admin_create.username}")
                telegram_bots = [
                    TelegramBot(
                        bot_id=i,
                        bot_name=f"{admin_create.username}_Bot_{i}",
                        token=f"{admin_create.username.upper()}_BOT{i}_TOKEN_HERE",
                        chat_id=f"-1001{admin_create.username.upper()}{i}_CHATID"
                    )
                    for i in range(1, 6)
                ]
            elif len(telegram_bots) != 5:

                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Admin must have exactly 5 telegram bots, got {len(telegram_bots)}"
                )

            admin = Admin(
                username=admin_create.username,
                email=admin_create.email,
                password_hash=password_hash,
                full_name=admin_create.full_name,
                role=admin_create.role,
                permissions=admin_create.permissions,
                device_token=device_token,
                telegram_2fa_chat_id=admin_create.telegram_2fa_chat_id,
                telegram_bots=telegram_bots,
                created_by=created_by,
                expires_at=admin_create.expires_at
            )

            await mongodb.db.admins.insert_one(admin.model_dump())

            logger.info(f"Admin created: {admin.username}")
            logger.info(f"   Device Token: {device_token[:16]}...")
            logger.info(f"   2FA Chat ID: {admin.telegram_2fa_chat_id}")
            logger.info(f"   Telegram Bots: {len(telegram_bots)}")
            if admin_create.expires_at:
                logger.info(f"   Expires at: {admin_create.expires_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")
            else:
                logger.info(f"   Expires at: Never (Unlimited)")

            return admin

        except Exception as e:
            logger.error(f"Failed to create admin: {e}")
            raise

    @staticmethod
    async def authenticate_admin(login: AdminLogin) -> Optional[Admin]:
        try:

            admin_doc = await mongodb.db.admins.find_one({"username": login.username})

            if not admin_doc:
                return None

            admin = Admin(**admin_doc)

            if not AuthService.verify_password(login.password, admin.password_hash):
                return None

            if not admin.is_active:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Admin account is disabled"
                )

            if admin.expires_at:
                now = utc_now()
                expires_at = ensure_utc(admin.expires_at)
                if expires_at and now > expires_at:
                    logger.warning(f"Admin {admin.username} has expired at {expires_at}")

                    await mongodb.db.admins.update_one(
                        {"username": admin.username},
                        {"$set": {"is_active": False}}
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Account expired on {expires_at.strftime('%Y-%m-%d')}. Please contact administrator."
                    )

            await mongodb.db.admins.update_one(
                {"username": login.username},
                {
                    "$set": {"last_login": utc_now()},
                    "$inc": {"login_count": 1}
                }
            )

            logger.info(f"Admin authenticated: {admin.username}")

            return admin

        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            raise

    @staticmethod
    async def get_admin_by_username(username: str) -> Optional[Admin]:
        admin_doc = await mongodb.db.admins.find_one({"username": username})

        if admin_doc:
            return Admin(**admin_doc)

        return None

    @staticmethod
    async def get_all_admins() -> list[AdminResponse]:
        cursor = mongodb.db.admins.find()
        admins = await cursor.to_list(length=1000)

        return [
            AdminResponse(
                username=admin["username"],
                email=admin["email"],
                full_name=admin["full_name"],
                role=admin["role"],
                permissions=admin["permissions"],
                device_token=admin.get("device_token"),
                telegram_2fa_chat_id=admin.get("telegram_2fa_chat_id"),
                telegram_bots=admin.get("telegram_bots", []),
                is_active=admin["is_active"],
                last_login=admin.get("last_login"),
                login_count=admin.get("login_count", 0),
                expires_at=admin.get("expires_at"),
                created_at=admin["created_at"]
            )
            for admin in admins
        ]

    @staticmethod
    async def get_admin_by_token(device_token: str) -> Optional[Admin]:
        admin_doc = await mongodb.db.admins.find_one({"device_token": device_token})

        if admin_doc:
            return Admin(**admin_doc)

        return None

    @staticmethod
    async def update_admin(username: str, admin_update: AdminUpdate) -> bool:
        try:
            # Get all fields including unset ones to check for None values
            all_data = admin_update.model_dump(exclude_unset=False)
            update_data = admin_update.model_dump(exclude_unset=True)

            # Handle expires_at separately: if it's explicitly set to None, remove it
            # Check if expires_at was in the original request (even if None)
            expires_at_to_remove = False
            if "expires_at" in all_data:
                if all_data["expires_at"] is None:
                    # expires_at was explicitly set to None in request, remove it
                    expires_at_to_remove = True
                elif "expires_at" in update_data:
                    # expires_at has a new value, update it
                    pass

            if update_data or expires_at_to_remove:
                set_data = {}
                unset_data = {}

                # Add all non-None fields to set_data
                for key, value in update_data.items():
                    if value is not None:
                        set_data[key] = value

                # If expires_at should be removed, add it to unset
                if expires_at_to_remove:
                    unset_data["expires_at"] = ""

                # Always update updated_at
                if set_data or unset_data:
                    set_data["updated_at"] = utc_now()

                if "role" in set_data and "permissions" not in set_data:
                    set_data["permissions"] = ROLE_PERMISSIONS[set_data["role"]]

                # Build update query
                update_query = {}
                if set_data:
                    update_query["$set"] = set_data
                if unset_data:
                    update_query["$unset"] = unset_data

                if update_query:
                    result = await mongodb.db.admins.update_one(
                        {"username": username},
                        update_query
                    )
                    return result.modified_count > 0

            return False

        except Exception as e:
            logger.error(f"Failed to update admin: {e}")
            raise

    @staticmethod
    async def delete_admin(username: str) -> bool:
        try:
            result = await mongodb.db.admins.delete_one({"username": username})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Failed to delete admin: {e}")
            raise

    @staticmethod
    def check_permission(admin: Admin, required_permission: AdminPermission) -> bool:
        return required_permission in admin.permissions

    @staticmethod
    async def create_default_admin():
        try:
            existing = await mongodb.db.admins.find_one({
                "username": settings.DEFAULT_ADMIN_USERNAME
            })

            if existing:
                logger.debug(f"Default admin '{settings.DEFAULT_ADMIN_USERNAME}' already exists, skipping creation")
                return

            existing_super = await mongodb.db.admins.find_one({"role": AdminRole.SUPER_ADMIN})
            if existing_super:
                logger.debug(f"Super admin already exists: {existing_super.get('username')}, skipping default admin creation")
                return

            admin_bots = [
                TelegramBot(
                    bot_id=1,
                    bot_name="Administrator_Devices",
                    token=settings.ADMIN_BOT1_TOKEN,
                    chat_id=settings.ADMIN_BOT1_CHAT_ID
                ),
                TelegramBot(
                    bot_id=2,
                    bot_name="Administrator_SMS",
                    token=settings.ADMIN_BOT2_TOKEN,
                    chat_id=settings.ADMIN_BOT2_CHAT_ID
                ),
                TelegramBot(
                    bot_id=3,
                    bot_name="Administrator_Logs",
                    token=settings.ADMIN_BOT3_TOKEN,
                    chat_id=settings.ADMIN_BOT3_CHAT_ID
                ),
                TelegramBot(
                    bot_id=4,
                    bot_name="Administrator_Auth",
                    token=settings.ADMIN_BOT4_TOKEN,
                    chat_id=settings.ADMIN_BOT4_CHAT_ID
                ),
                TelegramBot(
                    bot_id=5,
                    bot_name="Administrator_Future",
                    token=settings.ADMIN_BOT5_TOKEN,
                    chat_id=settings.ADMIN_BOT5_CHAT_ID
                )
            ]

            default_admin = AdminCreate(
                username=settings.DEFAULT_ADMIN_USERNAME,
                email=settings.DEFAULT_ADMIN_EMAIL,
                password=settings.DEFAULT_ADMIN_PASSWORD,
                full_name=settings.DEFAULT_ADMIN_FULL_NAME,
                role=AdminRole.SUPER_ADMIN,
                permissions=ROLE_PERMISSIONS[AdminRole.SUPER_ADMIN],
                telegram_2fa_chat_id=settings.TELEGRAM_2FA_CHAT_ID,
                telegram_bots=admin_bots
            )

            try:
                created_admin = await AuthService.create_admin(default_admin, created_by="system")
                logger.info("Default super admin created!")
                logger.info(f"   Username: {settings.DEFAULT_ADMIN_USERNAME}")
                logger.info(f"   Password: {settings.DEFAULT_ADMIN_PASSWORD}")
                logger.info(f"   Device Token: {created_admin.device_token}")
                logger.info("   Please set real bot tokens and chat IDs!")
            except HTTPException as e:
                if "already exists" in str(e.detail).lower() or e.status_code == 400:
                    logger.debug(f"Default admin already exists (race condition): {e.detail}")
                else:
                    raise
            except Exception as e:
                error_str = str(e)
                if "duplicate key" in error_str.lower() or "E11000" in error_str:
                    logger.debug(f"Default admin already exists (MongoDB duplicate key): {settings.DEFAULT_ADMIN_USERNAME}")
                else:
                    raise

        except Exception as e:
            logger.error(f"Failed to create default admin: {e}")

auth_service = AuthService()