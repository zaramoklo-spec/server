from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from typing import Optional
import logging
from datetime import datetime, timezone

from ..services.auth_service import auth_service
from ..models.admin_schemas import Admin, AdminPermission
from ..database import mongodb
from ..utils.datetime_utils import ensure_utc, utc_now

security = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)

async def get_current_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> Admin:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    token = credentials.credentials
    
    try:
        payload = await auth_service.verify_token(token)
    except Exception as e:
        logger.warning(f"Token verification failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token. Please login again.",
            headers={"X-Session-Expired": "true"}
        )
    
    username = payload.get("sub")
    token_session_id = payload.get("session_id")
    client_type = payload.get("client_type", "interactive")

    admin = await auth_service.get_admin_by_username(username)

    if not admin:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin not found",
            headers={"X-Session-Expired": "true"}
        )

    if not admin.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin account is disabled",
            headers={"X-Session-Expired": "true"}
        )

    if admin.expires_at:
        now = utc_now()
        expires_at = ensure_utc(admin.expires_at)
        if expires_at and now > expires_at:
            logger.warning(f"Admin {username} has expired at {expires_at}")

            await mongodb.db.admins.update_one(
                {"username": username},
                {"$set": {"is_active": False}}
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Account expired on {expires_at.strftime('%Y-%m-%d')}. Please contact administrator.",
                headers={"X-Session-Expired": "true"}
            )

    if client_type == "service":
        logger.info(f"ALLOW {username}: Service token (no session check)")
        return admin

    admin_session_id = getattr(admin, 'current_session_id', None)

    logger.info(f"Session Check for {username} (interactive):")
    logger.info(f"   Token session_id: {token_session_id}")
    logger.info(f"   DB session_id: {admin_session_id}")

    if admin_session_id is None:
        logger.warning(f"REJECT {username}: No session_id in database")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No active session. Please login again.",
            headers={"X-Session-Expired": "true"}
        )

    if not token_session_id:
        logger.warning(f"REJECT {username}: Token has no session_id")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token format. Please login again.",
            headers={"X-Session-Expired": "true"}
        )

    if token_session_id != admin_session_id:
        logger.warning(f"REJECT {username}: Session mismatch!")
        logger.warning(f"   Token has: {token_session_id[:20]}...")
        logger.warning(f"   DB has: {admin_session_id[:20]}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Another login detected from different location.",
            headers={"X-Session-Expired": "true"}
        )

    logger.info(f"ALLOW {username}: Session valid")
    return admin

async def get_optional_admin(
    request: Request
) -> Optional[Admin]:
    try:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return None

        token = auth_header.split(" ")[1]
        payload = await auth_service.verify_token(token)
        username = payload.get("sub")

        admin = await auth_service.get_admin_by_username(username)
        return admin if admin and admin.is_active else None

    except Exception as e:
        logger.debug(f"Optional admin auth failed: {e}")
        return None

def require_permission(required_permission: AdminPermission):
    async def permission_checker(
        current_admin: Admin = Depends(get_current_admin)
    ) -> Admin:
        if not auth_service.check_permission(current_admin, required_permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {required_permission.value} required"
            )
        return current_admin

    return permission_checker

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def get_user_agent(request: Request) -> str:
    return request.headers.get("User-Agent", "unknown")


async def authenticate_admin_token(token: str) -> Admin:
    """
    Utility helper for WebSocket and non-HTTP contexts to authenticate
    an admin using a bearer token and perform the same session checks
    as HTTP routes.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    return await get_current_admin(credentials)