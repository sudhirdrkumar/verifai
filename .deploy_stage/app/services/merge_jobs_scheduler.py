import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.services.merge_jobs_processor import process_merge_jobs

logger = logging.getLogger(__name__)


class MergeJobsScheduler:
    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.is_running = False

    def start(self) -> None:
        """Start the merge jobs scheduler"""
        if self.is_running:
            logger.warning("Merge jobs scheduler is already running")
            return

        try:
            # Process merge jobs every 10 seconds
            self.scheduler.add_job(
                process_merge_jobs,
                trigger=IntervalTrigger(seconds=10),
                id="process_merge_jobs",
                name="Process pending PDF merge jobs",
                replace_existing=True,
                max_instances=1,
            )
            self.scheduler.start()
            self.is_running = True
            logger.info("Merge jobs scheduler started (interval: 10 seconds)")
        except Exception as e:
            logger.error(f"Failed to start merge jobs scheduler: {e}")
            raise

    def stop(self) -> None:
        """Stop the merge jobs scheduler"""
        if not self.is_running:
            return

        try:
            self.scheduler.shutdown()
            self.is_running = False
            logger.info("Merge jobs scheduler stopped")
        except Exception as e:
            logger.error(f"Error stopping merge jobs scheduler: {e}")


merge_jobs_scheduler = MergeJobsScheduler()
