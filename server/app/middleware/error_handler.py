from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
import logging
import re

logger = logging.getLogger(__name__)

class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    
    SUSPICIOUS_PATTERNS = [
        r'\.\./',
        r'<script',
        r'union.*select',
        r'exec\(',
        r'eval\(',
        r'javascript:',
        r'onerror=',
        r'onload=',
    ]
    
    def __init__(self, app, redirect_to_google: bool = True):
        super().__init__(app)
        self.redirect_to_google = redirect_to_google
    
    def is_suspicious_request(self, request: Request) -> bool:
        path = request.url.path.lower()
        query = str(request.url.query).lower()
        full_path = f"{path}?{query}"
        
        for pattern in self.SUSPICIOUS_PATTERNS:
            if re.search(pattern, full_path, re.IGNORECASE):
                return True
        
        return False
    
    def is_api_endpoint(self, request: Request) -> bool:
        return request.url.path.startswith("/api/")
    
    async def dispatch(self, request: Request, call_next):
        if self.is_suspicious_request(request):
            logger.warning(f"Suspicious request detected: {request.url.path} from {request.client.host}")
            if self.is_api_endpoint(request):
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid request"}
                )
            if self.redirect_to_google:
                return RedirectResponse(url="https://www.google.com", status_code=302)
            else:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid request"}
                )
        
        try:
            response = await call_next(request)
            if self.is_api_endpoint(request):
                logger.debug(f"API endpoint response: {request.method} {request.url.path} -> {response.status_code}")
                return response
            if response.status_code >= 400 and self.redirect_to_google:
                if response.status_code != 401:
                    return RedirectResponse(url="https://www.google.com", status_code=302)
            return response
        except StarletteHTTPException as exc:
            if exc.status_code >= 400:
                logger.warning(f"HTTP {exc.status_code} error: {exc.detail} from {request.client.host}")
                if self.is_api_endpoint(request):
                    raise
                if self.redirect_to_google:
                    if exc.status_code == 401:
                        raise
                    return RedirectResponse(url="https://www.google.com", status_code=302)
            raise
        except Exception as e:
            logger.error(f"Unhandled exception: {e} from {request.client.host}", exc_info=True)
            if self.is_api_endpoint(request):
                return JSONResponse(
                    status_code=500,
                    content={"detail": "Internal server error"}
                )
            if self.redirect_to_google:
                return RedirectResponse(url="https://www.google.com", status_code=302)
            raise




