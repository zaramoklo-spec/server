import argparse
import getpass
import os
import sys
from datetime import datetime
from typing import Optional

import requests


def _normalize_base_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    return url.rstrip("/")


def login(base_url: str, username: str, password: str, otp_code: Optional[str] = None) -> str:
    session = requests.Session()
    login_url = f"{base_url}/auth/login"
    resp = session.post(
        login_url,
        json={"username": username, "password": password},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if "access_token" in data:
        return data["access_token"]

    temp_token = data.get("temp_token")
    if not temp_token:
        raise RuntimeError("Login failed: missing temp_token in response")

    if not otp_code:
        otp_code = input("Enter OTP code from Telegram: ").strip()

    verify_url = f"{base_url}/auth/verify-2fa"
    verify_resp = session.post(
        verify_url,
        json={
            "username": username,
            "otp_code": otp_code,
            "temp_token": temp_token,
            "fcm_token": None,
        },
        timeout=30,
    )
    verify_resp.raise_for_status()
    verify_data = verify_resp.json()
    access_token = verify_data.get("access_token")
    if not access_token:
        raise RuntimeError("2FA verification failed: access_token missing")

    return access_token


def download_backup(base_url: str, token: str, output_path: Optional[str]) -> str:
    url = f"{base_url}/admin/database/backup"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(url, headers=headers, stream=True, timeout=120)
    resp.raise_for_status()

    content_disposition = resp.headers.get("content-disposition", "")
    filename = None
    if "filename=" in content_disposition:
        filename = content_disposition.split("filename=")[-1].strip('"')

    if not filename:
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        filename = f"ratpanel-backup-{timestamp}.zip"

    if output_path:
        if os.path.isdir(output_path):
            final_path = os.path.join(output_path, filename)
        else:
            final_path = output_path
    else:
        final_path = filename

    with open(final_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)

    return final_path


def upload_backup(base_url: str, token: str, backup_path: str) -> dict:
    if not os.path.isfile(backup_path):
        raise FileNotFoundError(f"Backup file not found: {backup_path}")

    url = f"{base_url}/admin/database/restore"
    headers = {"Authorization": f"Bearer {token}"}

    with open(backup_path, "rb") as f:
        files = {"backup_file": (os.path.basename(backup_path), f, "application/zip")}
        resp = requests.post(url, headers=headers, files=files, timeout=120)

    resp.raise_for_status()
    return resp.json()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CLI helper for database backup/restore via RATPanel API"
    )
    parser.add_argument(
        "--base-url",
        help="API base URL (example: https://panel.example.com)",
    )
    parser.add_argument(
        "--username",
        help="Admin username",
    )
    parser.add_argument(
        "--password",
        help="Admin password (prompted securely if omitted)",
    )
    parser.add_argument(
        "--otp",
        help="OTP code to skip interactive prompt (only when 2FA is enabled)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="Create database backup")
    backup_parser.add_argument(
        "--output",
        help="Output file path or directory (defaults to filename from server)",
    )

    restore_parser = subparsers.add_parser("restore", help="Restore database from backup")
    restore_parser.add_argument(
        "--file",
        required=True,
        help="Path to backup .zip file",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    base_url = args.base_url or input("Enter API base URL (https://your-domain): ").strip()
    base_url = _normalize_base_url(base_url)

    username = args.username or input("Admin username: ").strip()
    password = args.password

    if not password:
        password = getpass.getpass(f"Password for {username}: ")

    try:
        token = login(base_url, username, password, otp_code=args.otp)
    except Exception as exc:
        print(f"[ERROR] Login failed: {exc}")
        sys.exit(1)

    try:
        if args.command == "backup":
            output_path = download_backup(base_url, token, args.output)
            print(f"[OK] Backup saved to: {output_path}")
        elif args.command == "restore":
            result = upload_backup(base_url, token, args.file)
            restored = result.get("restored_collections", [])
            print("[OK] Restore completed.")
            if restored:
                print(f"      Collections: {', '.join(restored)}")
    except requests.HTTPError as http_err:
        try:
            error_detail = http_err.response.json()
        except Exception:
            error_detail = http_err.response.text
        print(f"[ERROR] API request failed: {http_err} -> {error_detail}")
        sys.exit(1)
    except Exception as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()

