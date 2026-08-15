"""
Folder Sync Scheduler - Runs periodic folder sync jobs
"""
import asyncio
import logging
from datetime import datetime

from app.core.config import settings
from app.services.folder_sync_service import sync_folder

logger = logging.getLogger(__name__)


class FolderSyncScheduler:
    """Scheduler for periodic folder sync tasks"""

    def __init__(self):
        self._task = None
        self._running = False

    async def start(self):
        """Start the scheduler"""
        if not settings.folder_sync_enabled or not settings.folder_sync_path:
            logger.info("Folder sync disabled or path not configured")
            return

        if self._running:
            logger.warning("Folder sync scheduler already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Folder sync scheduler started (interval: {settings.folder_sync_interval_minutes} minutes)")

    async def stop(self):
        """Stop the scheduler"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Folder sync scheduler stopped")

    async def _run_loop(self):
        """Main scheduler loop"""
        while self._running:
            try:
                interval_seconds = settings.folder_sync_interval_minutes * 60

                logger.debug(f"Next folder sync in {settings.folder_sync_interval_minutes} minutes")
                await asyncio.sleep(interval_seconds)

                if not self._running:
                    break

                logger.info(f"Starting folder sync from {settings.folder_sync_path}")
                result = sync_folder(settings.folder_sync_path, actor_id="system")

                log_message = f"Folder sync completed at {datetime.utcnow()}: {result}"
                if result.get("success"):
                    logger.info(log_message)
                else:
                    logger.error(log_message)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Error in folder sync scheduler: {e}")
                # Continue loop even on error
                try:
                    await asyncio.sleep(60)  # Wait before retrying
                except asyncio.CancelledError:
                    break


# Global scheduler instance
folder_sync_scheduler = FolderSyncScheduler()
