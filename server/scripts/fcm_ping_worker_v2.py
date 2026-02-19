#!/usr/bin/env python3
"""
FCM Ping Worker V2 - Enterprise-grade distributed worker system
Supports thousands of devices with priority queues, rate limiting, and retry
"""
import asyncio
import logging
import sys
import os
import signal
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime, timedelta

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings
from app.database import connect_to_mongodb, mongodb
from app.services.firebase_service import firebase_service
from app.services.fcm_queue_service import (
    fcm_queue_service,
    FCMPingTask,
    Priority
)
from app.utils.datetime_utils import utc_now

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class FCMPingWorkerV2:
    """
    Enterprise-grade FCM Ping Worker
    
    Features:
    - Multiple concurrent workers
    - Priority queue processing
    - Rate limiting
    - Automatic retry
    - Metrics and monitoring
    - Graceful shutdown
    """
    
    def __init__(self, worker_id: str = "worker-1", concurrency: int = 10):
        self.worker_id = worker_id
        self.concurrency = concurrency
        self._is_running = False
        self._workers: List[asyncio.Task] = []
        self._initial_ping_done = False
        self._last_full_ping_time = None
        self._stats = {
            "processed": 0,
            "success": 0,
            "failed": 0,
            "retried": 0,
            "initial_ping_count": 0,
            "periodic_checks": 0,
            "suspicious_devices_found": 0,
            "start_time": None
        }
        
        # Scheduler configuration
        self.INITIAL_PING_DELAY = 10  # Wait 10 seconds after startup
        self.PERIODIC_CHECK_INTERVAL = 120  # Check suspicious devices every 2 minutes
        self.FULL_PING_INTERVAL = 7200  # Ping all devices every 2 hours (7200 seconds)
        self.SUSPICIOUS_DEVICE_THRESHOLD_MIN = 3  # 3 minutes
        self.SUSPICIOUS_DEVICE_THRESHOLD_MAX = 4  # 4 minutes
        self._last_full_ping_time = None
    
    async def initialize(self) -> bool:
        """Initialize services"""
        try:
            # Initialize queue service
            if not await fcm_queue_service.initialize():
                logger.error("❌ Failed to initialize queue service")
                return False
            
            # Connect to MongoDB
            await connect_to_mongodb()
            logger.info("✅ MongoDB connected")
            
            return True
        except Exception as e:
            logger.error(f"❌ Failed to initialize: {e}", exc_info=True)
            return False
    
    async def start(self):
        """Start the worker"""
        if not await self.initialize():
            logger.error("❌ Failed to initialize worker")
            return
        
        self._is_running = True
        self._stats["start_time"] = datetime.now()
        
        # Start multiple concurrent workers
        for i in range(self.concurrency):
            worker_task = asyncio.create_task(
                self._worker_loop(f"worker-{i+1}")
            )
            self._workers.append(worker_task)
        
        # Start metrics reporter
        metrics_task = asyncio.create_task(self._metrics_reporter())
        
        # Start scheduler (initial ping + periodic checks)
        scheduler_task = asyncio.create_task(self._scheduler_loop())
        
        logger.info(f"🚀 FCM Ping Worker V2 started: {self.worker_id}")
        logger.info(f"   Workers: {self.concurrency}")
        logger.info(f"   Batch size: {fcm_queue_service.BATCH_SIZE}")
        logger.info(f"   Initial ping delay: {self.INITIAL_PING_DELAY}s")
        logger.info(f"   Periodic check interval: {self.PERIODIC_CHECK_INTERVAL}s")
        logger.info(f"   Full ping interval: {self.FULL_PING_INTERVAL}s (2 hours)")
        
        try:
            # Wait for all workers
            await asyncio.gather(*self._workers, metrics_task, scheduler_task)
        except asyncio.CancelledError:
            logger.info("🛑 Worker cancelled")
        finally:
            await self.stop()
    
    async def _worker_loop(self, worker_name: str):
        """Main worker loop - processes tasks from queue"""
        logger.info(f"🔄 {worker_name} started")
        
        while self._is_running:
            try:
                # Dequeue tasks
                tasks = await fcm_queue_service.dequeue()
                
                if not tasks:
                    # No tasks, wait a bit
                    await asyncio.sleep(0.5)
                    continue
                
                # Process tasks in parallel
                await self._process_batch(tasks, worker_name)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ Error in {worker_name}: {e}", exc_info=True)
                await asyncio.sleep(1)
        
        logger.info(f"🛑 {worker_name} stopped")
    
    async def _process_batch(self, tasks: List[FCMPingTask], worker_name: str):
        """Process a batch of tasks in parallel"""
        if not tasks:
            return
        
        logger.info(f"📡 {worker_name} processing {len(tasks)} tasks...")
        start_time = asyncio.get_event_loop().time()
        
        # Create parallel ping tasks
        ping_tasks = []
        for task in tasks:
            ping_tasks.append(self._ping_device_task(task))
        
        # Execute all pings in parallel (VERY FAST!)
        results = await asyncio.gather(*ping_tasks, return_exceptions=True)
        
        # Process results
        success_count = 0
        failed_count = 0
        uninstalled_count = 0
        
        for i, result in enumerate(results):
            task = tasks[i]
            
            if isinstance(result, Exception):
                failed_count += 1
                await fcm_queue_service.mark_completed(task, False)
                logger.error(f"❌ {worker_name} failed to ping {task.device_id}: {result}")
            elif isinstance(result, dict):
                if result.get("success"):
                    success_count += 1
                    await fcm_queue_service.mark_completed(task, True)
                elif result.get("is_uninstalled"):
                    # Device was marked as uninstalled (all tokens failed)
                    failed_count += 1
                    uninstalled_count += 1
                    await fcm_queue_service.mark_completed(task, False)
                    logger.warning(f"⚠️ {worker_name} - Device {task.device_id} marked as UNINSTALLED (all tokens failed)")
                    logger.info(f"📋 {worker_name} - Device {task.device_id} result: {result}")
                else:
                    failed_count += 1
                    await fcm_queue_service.mark_completed(task, False)
                    logger.warning(f"⚠️ {worker_name} - Device {task.device_id} ping failed: {result.get('message', 'Unknown error')}")
            else:
                failed_count += 1
                await fcm_queue_service.mark_completed(task, False)
                logger.warning(f"⚠️ {worker_name} - Device {task.device_id} returned unexpected result: {type(result)}")
        
        elapsed = asyncio.get_event_loop().time() - start_time
        self._stats["processed"] += len(tasks)
        self._stats["success"] += success_count
        self._stats["failed"] += failed_count
        
        logger.info(
            f"✅ {worker_name} completed: {success_count} success, "
            f"{failed_count} failed ({uninstalled_count} uninstalled) in {elapsed:.2f}s"
        )
    
    async def _ping_device_task(self, task: FCMPingTask) -> Dict[str, Any]:
        """Send FCM ping to a device"""
        try:
            logger.debug(f"🔍 [WORKER] Starting ping task for device: {task.device_id}")
            
            # Rate limiting
            if fcm_queue_service._rate_limiter:
                async with fcm_queue_service._rate_limiter:
                    result = await firebase_service.ping_device(task.device_id)
            else:
                # No rate limiter, send directly
                result = await firebase_service.ping_device(task.device_id)
            
            # Log result details
            if result.get("is_uninstalled"):
                logger.warning(f"⚠️ [WORKER] Device {task.device_id} was marked as UNINSTALLED during ping")
            elif not result.get("success"):
                logger.warning(f"⚠️ [WORKER] Device {task.device_id} ping failed: {result.get('message', 'Unknown')}")
            
            return result
        except Exception as e:
            logger.error(f"❌ [WORKER] Error pinging device {task.device_id}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def _metrics_reporter(self):
        """Report metrics periodically"""
        while self._is_running:
            try:
                await asyncio.sleep(30)  # Report every 30 seconds
                
                metrics = await fcm_queue_service.get_metrics()
                queue_length = await fcm_queue_service.get_queue_length()
                
                uptime = (datetime.now() - self._stats["start_time"]).total_seconds() if self._stats["start_time"] else 0
                
                logger.info(
                    f"📊 Metrics - Queue: {queue_length}, "
                    f"Processed: {self._stats['processed']}, "
                    f"Success: {self._stats['success']}, "
                    f"Failed: {self._stats['failed']}, "
                    f"Initial: {self._stats['initial_ping_count']}, "
                    f"Checks: {self._stats['periodic_checks']}, "
                    f"Suspicious: {self._stats['suspicious_devices_found']}, "
                    f"Uptime: {int(uptime)}s"
                )
                
                if metrics:
                    logger.debug(f"   Detailed metrics: {metrics}")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ Error in metrics reporter: {e}")
    
    async def _scheduler_loop(self):
        """Scheduler loop: Initial ping + periodic checks + full ping every 2 hours"""
        try:
            # Wait before initial ping
            logger.info(f"⏳ Waiting {self.INITIAL_PING_DELAY}s before initial ping to all devices...")
            await asyncio.sleep(self.INITIAL_PING_DELAY)
            
            # Step 1: Ping ALL devices on startup
            await self._ping_all_devices_initial()
            self._last_full_ping_time = datetime.now()
            
            # Step 2: Periodic checks for suspicious devices + full ping every 2 hours
            while self._is_running:
                try:
                    now = datetime.now()
                    
                    # Check if 2 hours passed since last full ping
                    if self._last_full_ping_time:
                        time_since_last_full_ping = (now - self._last_full_ping_time).total_seconds()
                        if time_since_last_full_ping >= self.FULL_PING_INTERVAL:
                            logger.info(f"⏰ 2 hours passed - Pinging ALL devices again...")
                            await self._ping_all_devices_initial()
                            self._last_full_ping_time = now
                    
                    # Check suspicious devices
                    await self._check_suspicious_devices()
                    
                    await asyncio.sleep(self.PERIODIC_CHECK_INTERVAL)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"❌ Error in scheduler check: {e}", exc_info=True)
                    await asyncio.sleep(30)  # Wait 30 seconds on error
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"❌ Error in scheduler loop: {e}", exc_info=True)
    
    async def _ping_all_devices_initial(self):
        """Ping ALL devices (used for initial ping and periodic full ping every 2 hours)"""
        try:
            logger.info("📡 Starting ping to ALL devices...")
            
            # Get ALL devices (only filter deleted and uninstalled ones)
            # Don't filter by model - some devices might not have model set
            devices = await mongodb.db.devices.find(
                {
                    "is_deleted": {"$ne": True},
                    "$or": [
                        {"is_uninstalled": {"$ne": True}},
                        {"is_uninstalled": False},
                        {"is_uninstalled": {"$exists": False}}
                    ]
                },
                {"device_id": 1, "fcm_tokens": 1}
            ).to_list(length=None)
            
            logger.info(f"📥 Found {len(devices)} total devices (not deleted)")
            
            # Filter devices that have at least one FCM token
            device_ids_with_tokens = []
            devices_without_tokens = 0
            
            for device in devices:
                device_id = device.get("device_id")
                if not device_id:
                    continue
                
                fcm_tokens = device.get("fcm_tokens", [])
                
                # Check if device has valid FCM tokens
                if fcm_tokens:
                    # Check if at least one token is valid (not NO_FCM_TOKEN_*)
                    has_valid_token = False
                    for token in fcm_tokens:
                        if token and isinstance(token, str) and not token.startswith("NO_FCM_TOKEN"):
                            has_valid_token = True
                            break
                    
                    if has_valid_token:
                        device_ids_with_tokens.append(device_id)
                    else:
                        devices_without_tokens += 1
                else:
                    devices_without_tokens += 1
            
            logger.info(f"📊 Devices breakdown:")
            logger.info(f"   - Total devices: {len(devices)}")
            logger.info(f"   - Devices with valid FCM tokens: {len(device_ids_with_tokens)}")
            logger.info(f"   - Devices without valid tokens: {devices_without_tokens}")
            
            if not device_ids_with_tokens:
                logger.warning("⚠️ No devices with valid FCM tokens found for ping")
                if not self._initial_ping_done:
                    self._initial_ping_done = True
                return
            
            logger.info(f"📥 Enqueuing {len(device_ids_with_tokens)} devices for ping...")
            
            # Enqueue all devices with HIGH priority
            enqueued = await fcm_queue_service.enqueue(
                device_ids=device_ids_with_tokens,
                priority=Priority.HIGH,
                metadata={
                    "reason": "full_ping_all" if self._initial_ping_done else "initial_ping_all",
                    "triggered_by": "worker_scheduler",
                    "total_devices": len(device_ids_with_tokens)
                }
            )
            
            if not self._initial_ping_done:
                self._stats["initial_ping_count"] = enqueued
                self._initial_ping_done = True
            else:
                # Update stats for periodic full ping
                self._stats["initial_ping_count"] = enqueued
            
            logger.info(f"✅ Ping enqueued: {enqueued} devices (out of {len(device_ids_with_tokens)} with valid tokens)")
            
        except Exception as e:
            logger.error(f"❌ Failed to ping all devices: {e}", exc_info=True)
            if not self._initial_ping_done:
                self._initial_ping_done = True
    
    async def _check_suspicious_devices(self):
        """Check for suspicious devices (3-4 min no ping) and ping them"""
        try:
            self._stats["periodic_checks"] += 1
            
            now = utc_now()
            threshold_min = now - timedelta(minutes=self.SUSPICIOUS_DEVICE_THRESHOLD_MIN)
            threshold_max = now - timedelta(minutes=self.SUSPICIOUS_DEVICE_THRESHOLD_MAX)
            
            # Find suspicious devices (don't filter by model - ping all devices that need it)
            # Exclude uninstalled devices
            suspicious_devices = await mongodb.db.devices.find({
                "is_deleted": {"$ne": True},
                "$and": [
                    {
                        "$or": [
                            {"is_uninstalled": {"$ne": True}},
                            {"is_uninstalled": False},
                            {"is_uninstalled": {"$exists": False}}
                        ]
                    },
                    {
                        "last_ping": {
                            "$gte": threshold_max,
                            "$lte": threshold_min
                        }
                    },
                    {
                        "$or": [
                            {"fcm_ping_sent_at": {"$exists": False}},
                            {"fcm_ping_sent_at": {"$lt": threshold_min}}
                        ]
                    }
                ]
            }).to_list(length=None)
            
            if suspicious_devices:
                device_ids = [d["device_id"] for d in suspicious_devices if d.get("device_id")]
                
                logger.info(f"🔍 Found {len(device_ids)} suspicious devices (3-4 min no ping)")
                
                # Enqueue for FCM ping
                enqueued = await fcm_queue_service.enqueue(
                    device_ids=device_ids,
                    priority=Priority.HIGH,
                    metadata={
                        "reason": "suspicious_device",
                        "triggered_by": "worker_scheduler",
                        "check_number": self._stats["periodic_checks"]
                    }
                )
                
                # Update fcm_ping_sent_at
                await mongodb.db.devices.update_many(
                    {"device_id": {"$in": device_ids}},
                    {"$set": {"fcm_ping_sent_at": now}}
                )
                
                self._stats["suspicious_devices_found"] += len(device_ids)
                logger.info(f"✅ Enqueued {enqueued} suspicious devices for FCM ping")
            else:
                logger.debug(f"✅ No suspicious devices found (check #{self._stats['periodic_checks']})")
                
        except Exception as e:
            logger.error(f"❌ Error checking suspicious devices: {e}", exc_info=True)
    
    async def stop(self):
        """Stop the worker gracefully"""
        logger.info("🛑 Stopping worker...")
        self._is_running = False
        
        # Cancel all workers
        for worker in self._workers:
            worker.cancel()
        
        # Wait for workers to finish
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        
        logger.info("✅ Worker stopped")


async def main():
    """Main entry point"""
    # Get worker ID from environment or use default
    worker_id = os.getenv("WORKER_ID", "worker-1")
    concurrency = int(os.getenv("WORKER_CONCURRENCY", "10"))
    
    worker = FCMPingWorkerV2(worker_id=worker_id, concurrency=concurrency)
    
    # Setup signal handlers for graceful shutdown
    def signal_handler(sig, frame):
        logger.info("🛑 Received shutdown signal")
        asyncio.create_task(worker.stop())
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        await worker.start()
    except KeyboardInterrupt:
        logger.info("🛑 Interrupted by user")
        await worker.stop()
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(main())

