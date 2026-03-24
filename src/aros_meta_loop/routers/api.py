"""Meta Loop API endpoints."""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/meta-loop", tags=["meta-loop"])

# Global engine instance (set during startup)
_engine = None

def set_engine(engine):
    global _engine
    _engine = engine

def get_engine():
    if _engine is None:
        raise HTTPException(503, "Meta Loop engine not initialized")
    return _engine


# Request/Response models
class TriggerRequest(BaseModel):
    trigger: str = "manual"
    bot_id: str = "default"

class AdhocTriggerRequest(BaseModel):
    dry_run: bool = False
    skip_cadence: bool = True
    stop_after_step: int | None = None
    bot_id: str = "default"

class TriggerResponse(BaseModel):
    status: str
    message: str

class SignalRequest(BaseModel):
    source: str
    priority: str = "normal"
    payload: dict = {}

class EventWebhook(BaseModel):
    bot_id: str
    event_type: str
    session_id: str | None = None
    data: dict | None = None

class ApprovalResponse(BaseModel):
    status: str
    change_id: str


@router.post("/trigger", response_model=TriggerResponse)
async def trigger_cycle(req: TriggerRequest, background_tasks: BackgroundTasks):
    """Start a meta-loop cycle in the background."""
    engine = get_engine()
    if engine.is_running:
        return TriggerResponse(status="skipped", message="Cycle already running")

    async def run():
        await engine.run_cycle(req.trigger)

    background_tasks.add_task(run)
    return TriggerResponse(status="started", message=f"Cycle triggered: {req.trigger}")


@router.post("/trigger/adhoc")
async def trigger_adhoc(req: AdhocTriggerRequest):
    """Run a cycle synchronously with fine-tuning controls for debugging.

    Unlike /trigger (background), this returns the full cycle result inline.

    Options:
        dry_run: Run all steps but skip PERSIST and PLAN (no state changes).
        skip_cadence: Bypass rate limits (default True for adhoc).
        stop_after_step: Stop after step N (1=PERCEIVE, 2=SELF-MODEL, 3=CRITIQUE,
                         4=POLICY_REVISION, 5=IDENTITY, 6=PERSIST, 7=PLAN).
    """
    engine = get_engine()
    if engine.is_running:
        return {"status": "skipped", "reason": "cycle already running"}

    result = await engine.run_cycle(
        "adhoc",
        dry_run=req.dry_run,
        skip_cadence=req.skip_cadence,
        stop_after_step=req.stop_after_step,
    )
    return result


@router.get("/status")
async def get_status():
    """Get current meta-loop status, last cycle, and meta-goal scores."""
    engine = get_engine()
    status = engine.get_status()

    # Add latest L2 scores if available
    try:
        from aros_meta_loop.services.metrics import L1Collector, L2Evaluator
        collector = L1Collector()
        l1 = collector.collect(engine.bot_id)
        evaluator = L2Evaluator(state_manager=engine.state)
        l2 = evaluator.evaluate(l1)
        status["meta_goal_scores"] = l2
    except Exception as e:
        status["meta_goal_scores"] = None
        status["meta_goal_error"] = str(e)

    return status


@router.post("/signal")
async def inject_signal(req: SignalRequest):
    """Inject a signal into the hot queue."""
    engine = get_engine()
    engine.state.push_signal({
        "source": req.source,
        "priority": req.priority,
        "payload": req.payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return {"status": "queued", "source": req.source}


@router.get("/evolution-log")
async def get_evolution_log(limit: int = 50):
    """Get paginated evolution log."""
    engine = get_engine()
    entries = engine.state.read_evolution_log(limit=limit)
    return {"entries": entries, "count": len(entries)}


@router.get("/evolution-log/summary")
async def get_evolution_log_summary():
    """Get aggregate stats from evolution log."""
    engine = get_engine()
    entries = engine.state.read_evolution_log(limit=10000)
    task_gen_entries = [e for e in entries if e.get("type") == "task_generation"]

    total_tasks = sum(len(e.get("tasks_generated", [])) for e in task_gen_entries)
    total_dispatched = sum(
        e.get("trigger_results", {}).get("green_dispatched", 0) for e in task_gen_entries
    )
    total_queued = sum(
        e.get("trigger_results", {}).get("yellow_queued", 0) for e in task_gen_entries
    )

    green_count = sum(
        1
        for e in task_gen_entries
        for t in e.get("tasks_generated", [])
        if t.get("authority_level") == "GREEN"
    )
    yellow_count = sum(
        1
        for e in task_gen_entries
        for t in e.get("tasks_generated", [])
        if t.get("authority_level") == "YELLOW"
    )

    return {
        "total_cycles_with_tasks": len(task_gen_entries),
        "total_tasks_generated": total_tasks,
        "total_dispatched": total_dispatched,
        "total_queued": total_queued,
        "by_authority": {"GREEN": green_count, "YELLOW": yellow_count},
    }


@router.post("/approve/{change_id}")
async def approve_change(change_id: str):
    """Approve a pending HUMAN-REVIEW change."""
    engine = get_engine()
    review_dir = engine.state.state_dir / "pending-review"
    change_file = review_dir / f"{change_id}.json"

    if not change_file.exists():
        raise HTTPException(404, f"Change {change_id} not found")

    change_data = json.loads(change_file.read_text())
    change_data["status"] = "approved"
    change_data["approved_at"] = datetime.now(timezone.utc).isoformat()

    # Apply the change to policy
    policy = engine.state.read_policy()
    section = change_data.get("section", "")
    key = change_data.get("key", "")
    new_value = change_data.get("new_value", "")

    if section in policy and isinstance(policy[section], dict):
        # Try to preserve type
        old = policy[section].get(key)
        if isinstance(old, int):
            policy[section][key] = int(new_value)
        elif isinstance(old, float):
            policy[section][key] = float(new_value)
        else:
            policy[section][key] = new_value
        engine.state.write_snapshot("policy.toml", policy)

    # Remove from pending
    change_file.unlink()

    return ApprovalResponse(status="approved", change_id=change_id)


@router.get("/pending-approvals")
async def get_pending_approvals():
    """List changes awaiting human review."""
    engine = get_engine()
    review_dir = engine.state.state_dir / "pending-review"
    pending = []
    if review_dir.exists():
        for f in sorted(review_dir.glob("*.json")):
            try:
                pending.append(json.loads(f.read_text()))
            except Exception:
                continue
    return {"pending": pending, "count": len(pending)}


@router.get("/pending-task-approvals")
async def get_pending_task_approvals():
    """List YELLOW tasks awaiting human approval."""
    engine = get_engine()
    approvals = engine.state.get_pending_approvals()
    pending = [a for a in approvals if a.get("status") == "pending"]
    return {"pending": pending, "count": len(pending)}


@router.post("/pending-task-approvals/{approval_id}/approve")
async def approve_pending_task(approval_id: str, background_tasks: BackgroundTasks):
    """Approve a pending YELLOW task and trigger execution via HarnessTrigger."""
    engine = get_engine()
    result = engine.state.approve_task(approval_id)
    if result is None:
        raise HTTPException(404, f"Pending approval {approval_id} not found or already processed")

    # Trigger the task via HarnessTrigger
    from aros_meta_loop.services.harness_trigger import HarnessTrigger
    from aros_meta_loop.services.task_planner import PlannedTask, AuthorityLevel

    task_data = result.get("task", {})
    planned_task = PlannedTask(
        title=task_data.get("title", "Approved task"),
        description=task_data.get("description", ""),
        target_project=task_data.get("target_project", ""),
        authority_level=AuthorityLevel.GREEN,  # Approved → treat as GREEN for dispatch
        estimated_minutes=task_data.get("estimated_minutes", 15),
        goal_source=task_data.get("goal_source", ""),
    )

    trigger = HarnessTrigger()

    async def dispatch():
        trigger.trigger_harness_loop([planned_task])

    background_tasks.add_task(dispatch)

    return {"status": "approved", "task": task_data, "approval_id": approval_id}


@router.post("/pending-task-approvals/{approval_id}/reject")
async def reject_pending_task(approval_id: str, reason: str = ""):
    """Reject a pending YELLOW task."""
    engine = get_engine()
    result = engine.state.reject_task(approval_id, reason)
    if result is None:
        raise HTTPException(404, f"Pending approval {approval_id} not found or already processed")
    return {"status": "rejected", "reason": reason, "approval_id": approval_id}


@router.post("/event")
async def receive_event(event: EventWebhook):
    """Webhook: receive events from mini-claude-bot."""
    engine = get_engine()
    engine.emitter.emit_event(
        bot_id=event.bot_id,
        event_type=event.event_type,
        session_id=event.session_id,
        data=event.data,
    )
    return {"status": "recorded"}


@router.post("/nirmana")
async def set_nirmana_mode(activate: bool = True):
    """Activate or deactivate Nirmana autonomous driver mode."""
    engine = get_engine()
    if activate:
        return await engine.activate_nirmana()
    else:
        return await engine.deactivate_nirmana()
