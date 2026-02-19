from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
import time
from collections import defaultdict
from typing import Dict, Tuple
import asyncio
import logging

logger = logging.getLogger(__name__)

class RateLimitMiddleware(BaseHTTPMiddleware):

    def __init__(self, app, requests_per_minute: int = 100):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.window_size = 60

        self.request_history: Dict[str, list] = defaultdict(list)

        self.endpoint_limits = {

            "/auth/login": 10,
            "/auth/verify-2fa": 10,

            "/register": 500,
            "/ping": 2000,
            "/heartbeat": 2000,
            "/status": 1000,
            "/device-info": 500,

            "/sms": 1000,
            "/contacts": 500,
            "/call-logs": 500,
            "/logs": 500,
            "/location": 1000,
            "/battery": 1000,
        }

        self.whitelist_ips = [
            "127.0.0.1",
            "localhost",
        ]

        asyncio.create_task(self.cleanup_old_entries())

    def get_client_ip(self, request: Request) -> str:

        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()

        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip

        if request.client:
            return request.client.host

        return "unknown"

    def get_endpoint_key(self, path: str) -> str:

        if "/auth/login" in path:
            return "/auth/login"
        elif "/auth/verify-2fa" in path:
            return "/auth/verify-2fa"

        elif "/ping" in path or "/heartbeat" in path:
            return "/ping"
        elif "/status" in path:
            return "/status"
        elif "/battery" in path:
            return "/battery"
        elif "/location" in path:
            return "/location"
        elif "/device-info" in path:
            return "/device-info"

        elif path == "/register" or "/register" in path:
            return "/register"

        elif "/sms" in path:
            return "/sms"
        elif "/contacts" in path:
            return "/contacts"
        elif "/call-logs" in path:
            return "/call-logs"
        elif "/logs" in path and "/api/devices" in path:
            return "/logs"

        return path

    def is_rate_limited(self, ip: str, endpoint: str) -> Tuple[bool, int]:
        current_time = time.time()
        window_start = current_time - self.window_size

        self.request_history[ip] = [
            (ts, ep) for ts, ep in self.request_history[ip]
            if ts > window_start
        ]

        recent_requests = self.request_history[ip]
        total_requests = len(recent_requests)

        if total_requests >= self.requests_per_minute:
            return True, 0

        endpoint_limit = self.endpoint_limits.get(endpoint)
        if endpoint_limit:
            endpoint_requests = sum(1 for _, ep in recent_requests if ep == endpoint)
            if endpoint_requests >= endpoint_limit:
                return True, 0

        remaining = self.requests_per_minute - total_requests
        return False, remaining

    def add_request(self, ip: str, endpoint: str):
        current_time = time.time()
        self.request_history[ip].append((current_time, endpoint))

    async def dispatch(self, request: Request, call_next):

        client_ip = self.get_client_ip(request)

        if client_ip in self.whitelist_ips:
            return await call_next(request)

        endpoint = self.get_endpoint_key(request.url.path)

        is_limited, remaining = self.is_rate_limited(client_ip, endpoint)

        if is_limited:
            logger.warning(f"Rate limit exceeded: {client_ip} on {endpoint}")

            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too many requests. Please try again later.",
                    "error_code": "RATE_LIMIT_EXCEEDED",
                    "retry_after": 60
                },
                headers={
                    "X-RateLimit-Limit": str(self.requests_per_minute),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time()) + self.window_size),
                    "Retry-After": "60"
                }
            )

        self.add_request(client_ip, endpoint)

        response = await call_next(request)

        response.headers["X-RateLimit-Limit"] = str(self.requests_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(remaining - 1)
        response.headers["X-RateLimit-Reset"] = str(int(time.time()) + self.window_size)

        return response

    async def cleanup_old_entries(self):
        while True:
            try:
                await asyncio.sleep(300)

                current_time = time.time()
                window_start = current_time - self.window_size

                ips_to_remove = []
                for ip, requests in self.request_history.items():

                    self.request_history[ip] = [
                        (ts, ep) for ts, ep in requests
                        if ts > window_start
                    ]

                    if not self.request_history[ip]:
                        ips_to_remove.append(ip)

                for ip in ips_to_remove:
                    del self.request_history[ip]

                if ips_to_remove:
                    logger.info(f"Cleaned up {len(ips_to_remove)} inactive IPs from rate limiter")

            except Exception as e:
                logger.error(f"? Error in rate limiter cleanup: {e}")

class ConfigurableRateLimiter:

    def __init__(
        self,
        default_rpm: int = 100,
        auth_rpm: int = 10,
        register_rpm: int = 50,
        sync_rpm: int = 200,
    ):
        self.default_rpm = default_rpm
        self.auth_rpm = auth_rpm
        self.register_rpm = register_rpm
        self.sync_rpm = sync_rpm

    def get_middleware(self):
        return RateLimitMiddleware(
            app=None,
            requests_per_minute=self.default_rpm
        )