#!/usr/bin/env python3
"""
Script to merge FCM tokens from backup database to current database.
This script reads FCM tokens from a backup and merges them into the current database
without overwriting existing tokens.
"""

import sys
import os
from pathlib import Path
from typing import Dict, List, Any, Optional
import json
import logging
from datetime import datetime

# Add parent directory to path to import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from pymongo import MongoClient
from app.config import settings
from app.services.device_service import DeviceService

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def extract_mongodb_credentials(mongodb_url: str) -> Dict[str, str]:
    """Extract MongoDB credentials from connection string"""
    try:
        if mongodb_url.startswith("mongodb://"):
            url = mongodb_url.replace("mongodb://", "")
            if "@" in url:
                auth_part, rest = url.split("@", 1)
                if ":" in auth_part:
                    username, password = auth_part.split(":", 1)
                    # Extract host and port
                    if "/" in rest:
                        host_port, db_part = rest.split("/", 1)
                        if "?" in db_part:
                            db_name, params = db_part.split("?", 1)
                        else:
                            db_name = db_part
                            params = ""
                        
                        if ":" in host_port:
                            host, port = host_port.split(":", 1)
                        else:
                            host = host_port
                            port = "27017"
                    else:
                        if ":" in rest:
                            host, port = rest.split(":", 1)
                        else:
                            host = rest
                            port = "27017"
                        db_name = "RATPanel"
                        params = ""
                    
                    auth_source = "admin"
                    if "authSource=" in params:
                        auth_source = params.split("authSource=")[1].split("&")[0]
                    
                    return {
                        "username": username,
                        "password": password,
                        "host": host,
                        "port": port,
                        "database": db_name,
                        "auth_source": auth_source
                    }
    except Exception as e:
        logger.error(f"Error parsing MongoDB URL: {e}")
    
    return {
        "username": "ownerapps",
        "password": "9kiwlPwQ2ICpc1F",
        "host": "localhost",
        "port": "27017",
        "database": "RATPanel",
        "auth_source": "admin"
    }


def load_backup_data(backup_path: str) -> Optional[Dict[str, Any]]:
    """Load backup data from file or directory"""
    backup_path_obj = Path(backup_path)
    
    if not backup_path_obj.exists():
        logger.error(f"Backup path does not exist: {backup_path}")
        return None
    
    # Check if it's a tar.gz file
    if backup_path.endswith('.tar.gz'):
        logger.info("Detected tar.gz backup file. Extracting...")
        import tarfile
        import tempfile
        
        with tempfile.TemporaryDirectory() as temp_dir:
            with tarfile.open(backup_path, 'r:gz') as tar:
                tar.extractall(temp_dir)
            
            # Find the database directory
            db_dir = Path(temp_dir)
            for item in db_dir.iterdir():
                if item.is_dir() and (item / "devices.bson").exists():
                    backup_dir = item
                    break
            else:
                logger.error("Could not find database directory in backup")
                return None
            
            return {"type": "directory", "path": str(backup_dir)}
    
    # Check if it's a directory with BSON files
    elif backup_path_obj.is_dir():
        if (backup_path_obj / "devices.bson").exists():
            return {"type": "directory", "path": str(backup_path_obj)}
        else:
            logger.error(f"Backup directory does not contain devices.bson: {backup_path}")
            return None
    
    # Check if it's a JSON file
    elif backup_path.endswith('.json'):
        logger.info("Detected JSON backup file. Loading...")
        try:
            with open(backup_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return {"type": "json", "data": data}
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON file: {e}")
            return None
        except Exception as e:
            logger.error(f"Error reading JSON file: {e}")
            return None
    
    else:
        logger.error(f"Unknown backup format: {backup_path}")
        return None


def merge_fcm_tokens_from_backup(
    backup_path: str,
    current_mongodb_url: Optional[str] = None,
    backup_mongodb_url: Optional[str] = None,
    dry_run: bool = False
):
    """
    Merge FCM tokens from backup to current database.
    
    Args:
        backup_path: Path to backup file or directory
        current_mongodb_url: MongoDB URL for current database (defaults to config)
        backup_mongodb_url: MongoDB URL for backup database (if backup is in another DB)
        dry_run: If True, only show what would be merged without actually merging
    """
    
    # Connect to current database
    if current_mongodb_url:
        mongodb_url = current_mongodb_url
    else:
        # Try to get from environment first (Docker), then from config
        mongodb_url = os.getenv("MONGODB_URL") or settings.MONGODB_URL
    
    current_creds = extract_mongodb_credentials(mongodb_url)
    
    logger.info(f"Connecting to current database: {current_creds['host']}:{current_creds['port']}/{current_creds['database']}")
    
    # Use connection string directly for better compatibility
    try:
        current_client = MongoClient(mongodb_url)
        # Test connection
        current_client.admin.command('ping')
        logger.info("✅ Successfully connected to MongoDB")
    except Exception as e:
        logger.error(f"❌ Failed to connect to MongoDB: {e}")
        logger.error(f"   Connection string: {mongodb_url.replace(current_creds['password'], '***')}")
        return
    current_db = current_client[current_creds['database']]
    
    # Load backup data
    backup_info = load_backup_data(backup_path)
    if not backup_info:
        logger.error("Failed to load backup data")
        return
    
    backup_devices = {}
    
    if backup_info["type"] == "directory":
        # Load from BSON files using mongorestore or pymongo
        logger.info("Loading devices from BSON backup...")
        try:
            from bson import decode_file_iter
            devices_file = Path(backup_info["path"]) / "devices.bson"
            if devices_file.exists():
                with open(devices_file, 'rb') as f:
                    for doc in decode_file_iter(f):
                        device_id = doc.get('device_id')
                        if device_id:
                            backup_devices[device_id] = doc
                logger.info(f"Loaded {len(backup_devices)} devices from BSON backup")
            else:
                logger.error(f"devices.bson not found in {backup_info['path']}")
                return
        except ImportError:
            logger.error("bson module not available. Install pymongo to read BSON files.")
            return
        except Exception as e:
            logger.error(f"Error reading BSON file: {e}")
            return
    
    elif backup_info["type"] == "json":
        # Load from JSON
        logger.info("Loading devices from JSON backup...")
        json_data = backup_info["data"]
        
        # Check if it's a list of devices directly
        if isinstance(json_data, list):
            devices_data = json_data
        # Or if it's an object with "devices" key
        elif isinstance(json_data, dict):
            devices_data = json_data.get("devices", [])
        else:
            logger.error("Invalid JSON format")
            return
        
        if isinstance(devices_data, list):
            for device in devices_data:
                device_id = device.get('device_id')
                if device_id:
                    backup_devices[device_id] = device
        logger.info(f"Loaded {len(backup_devices)} devices from JSON backup")
    
    elif backup_mongodb_url:
        # Connect to backup database
        backup_creds = extract_mongodb_credentials(backup_mongodb_url)
        logger.info(f"Connecting to backup database: {backup_creds['host']}:{backup_creds['port']}/{backup_creds['database']}")
        backup_client = MongoClient(
            host=backup_creds['host'],
            port=int(backup_creds['port']),
            username=backup_creds['username'],
            password=backup_creds['password'],
            authSource=backup_creds['auth_source']
        )
        backup_db = backup_client[backup_creds['database']]
        
        # Load devices from backup database
        logger.info("Loading devices from backup database...")
        for device in backup_db.devices.find({}):
            device_id = device.get('device_id')
            if device_id:
                backup_devices[device_id] = device
        logger.info(f"Loaded {len(backup_devices)} devices from backup database")
        backup_client.close()
    
    if not backup_devices:
        logger.warning("No devices found in backup")
        return
    
    # Merge FCM tokens
    logger.info(f"\n{'='*60}")
    logger.info("Starting FCM token merge process...")
    logger.info(f"{'='*60}\n")
    
    stats = {
        "total_backup_devices": len(backup_devices),
        "devices_with_tokens": 0,
        "tokens_merged": 0,
        "devices_updated": 0,
        "devices_not_found": 0,
        "errors": 0
    }
    
    for device_id, backup_device in backup_devices.items():
        try:
            # Get FCM tokens from backup
            backup_tokens = []
            
            # Check for fcm_tokens array
            if 'fcm_tokens' in backup_device:
                tokens = backup_device['fcm_tokens']
                if isinstance(tokens, list):
                    backup_tokens = [t for t in tokens if DeviceService._is_valid_fcm_token(t)]
                elif isinstance(tokens, str) and DeviceService._is_valid_fcm_token(tokens):
                    backup_tokens = [tokens]
            
            # Also check for fcm_token (singular)
            elif 'fcm_token' in backup_device:
                token = backup_device['fcm_token']
                if DeviceService._is_valid_fcm_token(token):
                    backup_tokens = [token]
            
            if not backup_tokens:
                continue
            
            stats["devices_with_tokens"] += 1
            
            # Get current device
            current_device = current_db.devices.find_one({"device_id": device_id})
            
            if not current_device:
                logger.warning(f"Device {device_id} not found in current database")
                stats["devices_not_found"] += 1
                continue
            
            # Get current tokens
            current_tokens = []
            if 'fcm_tokens' in current_device:
                tokens = current_device['fcm_tokens']
                if isinstance(tokens, list):
                    current_tokens = [t for t in tokens if DeviceService._is_valid_fcm_token(t)]
                elif isinstance(tokens, str) and DeviceService._is_valid_fcm_token(tokens):
                    current_tokens = [tokens]
            
            # Merge tokens (add backup tokens that don't exist in current)
            tokens_to_add = []
            for backup_token in backup_tokens:
                if backup_token not in current_tokens:
                    tokens_to_add.append(backup_token)
            
            if tokens_to_add:
                if dry_run:
                    logger.info(f"[DRY RUN] Would merge {len(tokens_to_add)} token(s) for device {device_id}")
                    logger.info(f"  Current tokens: {len(current_tokens)}")
                    logger.info(f"  Backup tokens: {len(backup_tokens)}")
                    logger.info(f"  Tokens to add: {len(tokens_to_add)}")
                else:
                    # Merge tokens using $addToSet
                    result = current_db.devices.update_one(
                        {"device_id": device_id},
                        {
                            "$addToSet": {
                                "fcm_tokens": {"$each": tokens_to_add}
                            }
                        }
                    )
                    
                    if result.modified_count > 0:
                        logger.info(f"✅ Merged {len(tokens_to_add)} token(s) for device {device_id}")
                        logger.info(f"   Current: {len(current_tokens)} → New: {len(current_tokens) + len(tokens_to_add)}")
                        stats["devices_updated"] += 1
                        stats["tokens_merged"] += len(tokens_to_add)
                    else:
                        logger.warning(f"⚠️ No tokens merged for device {device_id} (may already exist)")
            else:
                logger.debug(f"⏭️ Device {device_id} - all backup tokens already exist")
        
        except Exception as e:
            logger.error(f"❌ Error processing device {device_id}: {e}", exc_info=True)
            stats["errors"] += 1
    
    # Print summary
    logger.info(f"\n{'='*60}")
    logger.info("Merge Summary:")
    logger.info(f"{'='*60}")
    logger.info(f"Total devices in backup: {stats['total_backup_devices']}")
    logger.info(f"Devices with FCM tokens: {stats['devices_with_tokens']}")
    logger.info(f"Devices updated: {stats['devices_updated']}")
    logger.info(f"Tokens merged: {stats['tokens_merged']}")
    logger.info(f"Devices not found: {stats['devices_not_found']}")
    logger.info(f"Errors: {stats['errors']}")
    logger.info(f"{'='*60}\n")
    
    current_client.close()


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Merge FCM tokens from backup to current database"
    )
    parser.add_argument(
        "backup_path",
        nargs="?",
        help="Path to backup file (.tar.gz, .json) or directory containing BSON files (optional - will auto-detect if not provided)"
    )
    parser.add_argument(
        "--current-db",
        help="MongoDB URL for current database (defaults to config.py)",
        default=None
    )
    parser.add_argument(
        "--backup-db",
        help="MongoDB URL for backup database (if backup is in another database)",
        default=None
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be merged without actually merging"
    )
    
    args = parser.parse_args()
    
    # Auto-detect backup file if not provided
    backup_path = args.backup_path
    if not backup_path:
        # Look for backup files in current directory and parent directory
        script_dir = Path(__file__).parent
        server_dir = script_dir.parent
        root_dir = server_dir.parent
        
        # Common backup file names (priority order)
        possible_names = [
            "RATPanel.devices.json",
            "devices.json",
            "backup.json"
        ]
        
        # Search in scripts directory, server directory, and root
        search_dirs = [script_dir, server_dir, root_dir]
        
        for search_dir in search_dirs:
            for name in possible_names:
                file_path = search_dir / name
                if file_path.exists():
                    backup_path = str(file_path)
                    logger.info(f"🔍 Auto-detected backup file: {backup_path}")
                    break
            if backup_path:
                break
        
        # Also try glob patterns
        if not backup_path:
            for search_dir in search_dirs:
                for pattern in ["*.devices.json", "RATPanel*.json"]:
                    matches = list(search_dir.glob(pattern))
                    if matches:
                        backup_path = str(matches[0])
                        logger.info(f"🔍 Auto-detected backup file: {backup_path}")
                        break
                if backup_path:
                    break
        
        if not backup_path:
            logger.error("❌ No backup file found. Please specify backup_path or place RATPanel.devices.json in server/scripts directory.")
            logger.info("💡 Usage: python scripts/merge_fcm_tokens.py [backup_file_path]")
            return
    
    if args.dry_run:
        logger.info("🔍 DRY RUN MODE - No changes will be made\n")
    
    merge_fcm_tokens_from_backup(
        backup_path=backup_path,
        current_mongodb_url=args.current_db,
        backup_mongodb_url=args.backup_db,
        dry_run=args.dry_run
    )


if __name__ == "__main__":
    main()

