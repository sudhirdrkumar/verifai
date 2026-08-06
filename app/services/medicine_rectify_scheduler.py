import asyncio
import logging
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.core.config import settings
from app.db.session import SessionLocal

_ADVISORY_LOCK_KEY = 982734611


class MedicineRectifyScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._logger = logging.getLogger(__name__)
        self._repo_root = Path(__file__).resolve().parents[2]
        self._script_path = self._repo_root / "scripts" / "rectify_medicine_catalog.py"

    def start(self) -> None:
        if not bool(getattr(settings, "medicine_rectify_scheduler_enabled", True)):
            self._logger.info("Medicine rectify scheduler is disabled")
            return
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="medicine-rectify-scheduler")
        self._logger.info("Medicine rectify scheduler started")

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=15)
        except asyncio.TimeoutError:
            self._task.cancel()
        except Exception:
            self._logger.exception("Error while stopping medicine rectify scheduler")
        finally:
            self._task = None

    async def _run_loop(self) -> None:
        tz = self._resolve_timezone()
        while not self._stop_event.is_set():
            now = datetime.now(tz)
            next_run = self._next_run_at(
                now,
                hour=int(getattr(settings, "medicine_rectify_scheduler_hour", 1) or 1),
                minute=int(getattr(settings, "medicine_rectify_scheduler_minute", 0) or 0),
            )
            delay_seconds = max((next_run - now).total_seconds(), 1.0)
            self._logger.info(
                "Next medicine rectify run at %s (%ss)",
                next_run.isoformat(),
                int(delay_seconds),
            )

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay_seconds)
                break
            except asyncio.TimeoutError:
                pass

            await asyncio.to_thread(self._run_once)

    def _resolve_timezone(self) -> ZoneInfo:
        tz_name = str(getattr(settings, "medicine_rectify_scheduler_tz", "Asia/Kolkata") or "Asia/Kolkata").strip()
        try:
            return ZoneInfo(tz_name)
        except Exception:
            self._logger.warning("Invalid scheduler timezone '%s', falling back to UTC", tz_name)
            return ZoneInfo("UTC")

    @staticmethod
    def _next_run_at(now: datetime, hour: int, minute: int) -> datetime:
        h = min(max(int(hour), 0), 23)
        m = min(max(int(minute), 0), 59)
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate = candidate + timedelta(days=1)
        return candidate

    def _run_once(self) -> None:
        if not self._script_path.exists():
            self._logger.error("Medicine rectify script not found at %s", self._script_path)
            return

        if not self._acquire_lock():
            self._logger.info("Medicine rectify job skipped because another worker holds the lock")
            return

        try:
            report_dir = self._repo_root / "artifacts" / "scheduler"
            report_dir.mkdir(parents=True, exist_ok=True)
            report_name = f"medicine_rectify_report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_apply.json"
            report_path = report_dir / report_name

            cmd = [
                sys.executable,
                str(self._script_path),
                "--apply",
                "--min-score",
                str(float(getattr(settings, "medicine_rectify_min_score", 0.82) or 0.82)),
                "--sleep-ms",
                str(int(getattr(settings, "medicine_rectify_sleep_ms", 0) or 0)),
                "--report",
                str(report_path),
            ]
            timeout_seconds = max(int(getattr(settings, "medicine_rectify_timeout_seconds", 10800) or 10800), 300)
            self._logger.info("Starting medicine rectify job")
            completed = subprocess.run(
                cmd,
                cwd=str(self._repo_root),
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
            stdout_tail = self._tail(completed.stdout)
            stderr_tail = self._tail(completed.stderr)
            if completed.returncode == 0:
                self._logger.info(
                    "Medicine rectify job completed successfully. report=%s output=%s",
                    report_path,
                    stdout_tail,
                )
            else:
                self._logger.error(
                    "Medicine rectify job failed (code=%s). stdout=%s stderr=%s",
                    completed.returncode,
                    stdout_tail,
                    stderr_tail,
                )
        except subprocess.TimeoutExpired:
            self._logger.error("Medicine rectify job timed out")
        except Exception:
            self._logger.exception("Medicine rectify job crashed")
        finally:
            self._release_lock()

    @staticmethod
    def _tail(value: str, max_len: int = 400) -> str:
        text_value = (value or "").strip()
        if len(text_value) <= max_len:
            return text_value
        return text_value[-max_len:]

    def _acquire_lock(self) -> bool:
        try:
            with SessionLocal() as db:
                got = db.execute(
                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": _ADVISORY_LOCK_KEY},
                ).scalar()
                return bool(got)
        except Exception:
            self._logger.exception("Failed to acquire advisory lock for medicine rectify job")
            return False

    def _release_lock(self) -> None:
        try:
            with SessionLocal() as db:
                db.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": _ADVISORY_LOCK_KEY},
                )
                db.commit()
        except Exception:
            self._logger.exception("Failed to release advisory lock for medicine rectify job")


medicine_rectify_scheduler = MedicineRectifyScheduler()
