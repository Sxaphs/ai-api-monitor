from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import SessionLocal, init_db
from .scheduler import create_scheduler
from .seed import seed_targets
from .web import router

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def create_app(
    *, start_scheduler: bool | None = None, initialize_database: bool = True
) -> FastAPI:
    scheduler_enabled = settings.scheduler_enabled if start_scheduler is None else start_scheduler

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if initialize_database:
            init_db()
            with SessionLocal() as session:
                seed_targets(session)
        scheduler = create_scheduler(SessionLocal) if scheduler_enabled else None
        app.state.scheduler = scheduler
        if scheduler:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler:
                scheduler.shutdown(wait=False)
            app.state.scheduler = None

    app = FastAPI(title="AI API 中转站竞品监测系统", version="1.0.0", lifespan=lifespan)
    app.state.scheduler = None
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )
    app.include_router(router)
    return app


app = create_app()
