import asyncio
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any
from ..utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)

class BackupService:
    def __init__(self, mongodb_url: str, db_name: str, backup_dir: str = "/app/backups"):
        self.mongodb_url = mongodb_url
        self.db_name = db_name
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        
    def _extract_mongo_credentials(self) -> Dict[str, str]:
        """Extract MongoDB credentials from connection string"""
        # Parse mongodb://username:password@host:port/database?authSource=admin
        try:
            if self.mongodb_url.startswith("mongodb://"):
                url = self.mongodb_url.replace("mongodb://", "")
                if "@" in url:
                    auth_part, rest = url.split("@", 1)
                    if ":" in auth_part:
                        username, password = auth_part.split(":", 1)
                        # Extract host and port
                        if "/" in rest:
                            host_port, db_part = rest.split("/", 1)
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
                        
                        return {
                            "username": username,
                            "password": password,
                            "host": host,
                            "port": port,
                            "auth_source": "admin"
                        }
        except Exception as e:
            logger.error(f"Error parsing MongoDB URL: {e}")
        
        # Fallback: try to get from environment or use defaults
        return {
            "username": os.getenv("MONGO_INITDB_ROOT_USERNAME", "ownerapps"),
            "password": os.getenv("MONGO_INITDB_ROOT_PASSWORD", ""),
            "host": "mongodb",
            "port": "27017",
            "auth_source": "admin"
        }
    
    async def create_backup(self) -> Dict[str, Any]:
        """Create a MongoDB backup using mongodump"""
        try:
            credentials = self._extract_mongo_credentials()
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_name = f"{self.db_name}_backup_{timestamp}"
            backup_path = self.backup_dir / backup_name
            
            logger.info(f"📦 Starting MongoDB backup: {backup_name}")
            
            # Build mongodump command
            cmd = [
                "mongodump",
                "--host", f"{credentials['host']}:{credentials['port']}",
                "--username", credentials["username"],
                "--password", credentials["password"],
                "--authenticationDatabase", credentials["auth_source"],
                "--db", self.db_name,
                "--out", str(backup_path)
            ]
            
            # Run mongodump in executor to avoid blocking
            loop = asyncio.get_event_loop()
            process = await loop.run_in_executor(
                None,
                subprocess.run,
                cmd,
                {
                    "capture_output": True,
                    "text": True,
                    "check": False
                }
            )
            
            if process.returncode == 0:
                # Create archive (tar.gz) for easier storage
                archive_name = f"{backup_name}.tar.gz"
                archive_path = self.backup_dir / archive_name
                
                # Compress backup directory
                tar_cmd = [
                    "tar",
                    "-czf",
                    str(archive_path),
                    "-C",
                    str(self.backup_dir),
                    backup_name
                ]
                
                tar_process = await loop.run_in_executor(
                    None,
                    subprocess.run,
                    tar_cmd,
                    {
                        "capture_output": True,
                        "text": True,
                        "check": False
                    }
                )
                
                if tar_process.returncode == 0:
                    # Remove uncompressed backup directory
                    import shutil
                    shutil.rmtree(backup_path, ignore_errors=True)
                    
                    # Get file size
                    file_size = archive_path.stat().st_size
                    file_size_mb = file_size / (1024 * 1024)
                    
                    logger.info(f"✅ Backup created successfully: {archive_name} ({file_size_mb:.2f} MB)")
                    
                    return {
                        "success": True,
                        "backup_name": archive_name,
                        "backup_path": str(archive_path),
                        "file_size_mb": round(file_size_mb, 2),
                        "timestamp": timestamp
                    }
                else:
                    logger.error(f"❌ Failed to compress backup: {tar_process.stderr}")
                    return {
                        "success": False,
                        "error": f"Compression failed: {tar_process.stderr}"
                    }
            else:
                logger.error(f"❌ Backup failed: {process.stderr}")
                return {
                    "success": False,
                    "error": process.stderr
                }
                
        except Exception as e:
            logger.error(f"❌ Error creating backup: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }
    
    async def cleanup_old_backups(self, keep_days: int = 7):
        """Remove backups older than keep_days"""
        try:
            cutoff_time = utc_now().timestamp() - (keep_days * 24 * 60 * 60)
            removed_count = 0
            
            for backup_file in self.backup_dir.glob("*.tar.gz"):
                if backup_file.stat().st_mtime < cutoff_time:
                    backup_file.unlink()
                    removed_count += 1
                    logger.info(f"🗑️ Removed old backup: {backup_file.name}")
            
            if removed_count > 0:
                logger.info(f"🧹 Cleaned up {removed_count} old backup(s)")
            
            return removed_count
        except Exception as e:
            logger.error(f"❌ Error cleaning up old backups: {e}", exc_info=True)
            return 0








