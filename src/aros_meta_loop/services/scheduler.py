"""APScheduler-based meta-loop trigger."""
import logging
import asyncio
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_engine = None


def start_scheduler(engine) -> None:
    """Start the meta-loop scheduler."""
    global _scheduler, _engine
    _engine = engine

    cadence = engine.state.read_cadence()
    mode = cadence.get("mode", "balanced")
    cron_config = cadence.get("cron", {})

    if mode == "frozen":
        logger.info("Cadence mode is frozen — scheduler not started")
        return

    interval_hours = cron_config.get("normal_interval_hours", 1)

    if mode == "aggressive":
        interval_minutes = cron_config.get("away_interval_minutes", 5)
        interval_seconds = interval_minutes * 60
    else:
        interval_seconds = interval_hours * 3600

    # In aggressive mode (/away), fire immediately then every 5min
    # In balanced mode (normal), wait 1 hour before first cycle
    fire_immediately = mode == "aggressive"

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _trigger_cycle,
        trigger=IntervalTrigger(seconds=interval_seconds),
        id="meta_loop_cycle",
        name="Meta Loop Cycle",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) if fire_immediately else None,
    )
    _scheduler.start()
    logger.info(f"Scheduler started: mode={mode}, interval={interval_seconds}s, fire_now={fire_immediately}")


def update_schedule(mode: str) -> None:
    """Update scheduler interval based on mode change."""
    global _scheduler, _engine
    if not _scheduler or not _engine:
        return

    cadence = _engine.state.read_cadence()
    cron_config = cadence.get("cron", {})

    if mode == "frozen":
        _scheduler.pause_job("meta_loop_cycle")
        logger.info("Scheduler paused (frozen mode)")
        return

    if mode == "aggressive":
        interval_minutes = cron_config.get("away_interval_minutes", 5)
        interval_seconds = interval_minutes * 60
    elif mode == "conservative":
        interval_hours = cron_config.get("normal_interval_hours", 1) * 2
        interval_seconds = interval_hours * 3600
    else:  # balanced, manual
        interval_hours = cron_config.get("normal_interval_hours", 1)
        interval_seconds = interval_hours * 3600

    # When switching to aggressive (/away), fire immediately then repeat
    fire_immediately = mode == "aggressive"

    _scheduler.reschedule_job(
        "meta_loop_cycle",
        trigger=IntervalTrigger(seconds=interval_seconds),
    )

    if fire_immediately:
        _scheduler.modify_job(
            "meta_loop_cycle",
            next_run_time=datetime.now(timezone.utc),
        )

    logger.info(f"Scheduler updated: mode={mode}, interval={interval_seconds}s, fire_now={fire_immediately}")


def stop_scheduler() -> None:
    """Stop the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")


def _trigger_cycle() -> None:
    """Background job: trigger a meta-loop cycle."""
    if not _engine:
        return
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_engine.run_cycle("scheduled"))
        loop.close()
    except Exception as e:
        logger.error(f"Scheduled cycle failed: {e}")
