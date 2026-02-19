from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from ..config import settings, BASE_DIR

logger = logging.getLogger(__name__)

DEFAULT_KEY_PATH = BASE_DIR.parent / "key"


class LeakLookupError(Exception):
    """Custom exception for leak lookup errors."""


class LeakLookupService:
    def __init__(self) -> None:
        self._api_url = settings.LEAKOSINT_API_URL.rstrip("/") + "/"
        self._token = settings.LEAKOSINT_API_TOKEN or self._load_token_from_file()

    def _load_token_from_file(self) -> Optional[str]:
        key_path = settings.LEAKOSINT_KEY_FILE

        candidate_paths = []
        if key_path:
            candidate_paths.append(Path(key_path))
        candidate_paths.append(DEFAULT_KEY_PATH)

        for path in candidate_paths:
            try:
                if path and path.exists():
                    token = path.read_text(encoding="utf-8").strip()
                    if token:
                        logger.info(f"Loaded LeakOSINT token from {path}")
                        return token
            except Exception as exc:
                logger.warning(f"Failed to read LeakOSINT token from {path}: {exc}")
        return None

    async def lookup(
        self,
        query: str,
        limit: int = 100,
        lang: str = "en",
        response_type: str = "json",
        bot_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        token = self._token or settings.LEAKOSINT_API_TOKEN
        if not token:
            raise LeakLookupError("LeakOSINT API token is not configured.")

        payload: Dict[str, Any] = {
            "token": token,
            "request": query,
            "limit": limit,
            "lang": lang,
            "type": response_type,
        }
        if bot_name:
            payload["bot_name"] = bot_name

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(self._api_url, json=payload)
        except httpx.RequestError as exc:
            logger.error(f"LeakOSINT request error: {exc}")
            raise LeakLookupError("Failed to reach LeakOSINT API.") from exc

        if response.status_code >= 400:
            logger.error(
                "LeakOSINT API returned %s: %s",
                response.status_code,
                response.text,
            )
            raise LeakLookupError(
                f"LeakOSINT API error: HTTP {response.status_code}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            logger.error(f"Invalid JSON from LeakOSINT API: {response.text}")
            raise LeakLookupError("LeakOSINT API returned invalid JSON.") from exc

        if isinstance(data, dict) and data.get("Error code"):
            error_msg = data.get("Error code") or "Unknown error"
            logger.warning(f"LeakOSINT API reported error: {error_msg}")
            raise LeakLookupError(error_msg)

        return data


leak_lookup_service = LeakLookupService()





