"""FastAPI application for AROS Meta Loop."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    from aros_meta_loop.config import init_state_dir
    from aros_meta_loop.db.engine import get_db
    from aros_meta_loop.services.engine import MetaLoopEngine
    from aros_meta_loop.services.state_manager import StateManager
    from aros_meta_loop.routers.api import set_engine
    from aros_meta_loop.services.scheduler import start_scheduler, stop_scheduler

    init_state_dir()
    get_db()

    state_mgr = StateManager()
    engine = MetaLoopEngine(state_manager=state_mgr)
    set_engine(engine)
    start_scheduler(engine)

    yield

    stop_scheduler()


app = FastAPI(
    title="AROS Meta Loop",
    description="Autonomous self-improvement engine",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from aros_meta_loop.routers.api import router as meta_loop_router
app.include_router(meta_loop_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "aros-meta-loop"}
