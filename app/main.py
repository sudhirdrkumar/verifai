from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import settings
from app.db.migrations import run_pending_migrations
from app.middleware.request_monitoring import RequestMonitoringMiddleware, TransactionGuardMiddleware
from app.services.medicine_rectify_scheduler import medicine_rectify_scheduler
from app.services.folder_sync_scheduler import folder_sync_scheduler

app = FastAPI(title=settings.app_name)

# Add monitoring middleware (must be added before routers)
app.add_middleware(TransactionGuardMiddleware)
app.add_middleware(RequestMonitoringMiddleware)

app.include_router(api_router, prefix=settings.api_v1_prefix)

WEB_ROOT = Path(__file__).resolve().parent / "web"
MONITOR_HTML_PATH = WEB_ROOT / "monitor.html"
QC_WEB_ROOT = WEB_ROOT / "qc"
QC_LOGIN_HTML_PATH = QC_WEB_ROOT / "login.html"
QC_WORKSPACE_HTML_PATH = QC_WEB_ROOT / "workspace.html"

app.mount("/qc/public", StaticFiles(directory=str(QC_WEB_ROOT / "public")), name="qc_public")


@app.on_event("startup")
async def on_startup() -> None:
    run_pending_migrations()
    medicine_rectify_scheduler.start()
    await folder_sync_scheduler.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await medicine_rectify_scheduler.stop()
    await folder_sync_scheduler.stop()


@app.get("/")
def read_root() -> RedirectResponse:
    return RedirectResponse(url="/qc/login", status_code=307)


@app.get("/monitor", response_class=HTMLResponse)
def monitor_ui() -> str:
    return MONITOR_HTML_PATH.read_text(encoding="utf-8")


@app.get("/qc")
def qc_index() -> RedirectResponse:
    return RedirectResponse(url="/qc/login")


@app.get("/qc/admin")
def admin_ui() -> RedirectResponse:
    return RedirectResponse(url="/qc/admin/dashboard")


@app.get("/qc/login", response_class=HTMLResponse)
def login_ui() -> str:
    return QC_LOGIN_HTML_PATH.read_text(encoding="utf-8")


@app.get("/qc/{path:path}", response_class=HTMLResponse)
def qc_catch_all(path: str) -> HTMLResponse:
    """Catch-all route for all /qc/* paths - serves workspace for doctor, dashboard, etc."""
    if path == "" or path == "login":
        return HTMLResponse(
            content=QC_LOGIN_HTML_PATH.read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    return HTMLResponse(
        content=QC_WORKSPACE_HTML_PATH.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

@app.get("/qc/workspace", response_class=HTMLResponse)
def workspace_ui() -> str:
    return QC_WORKSPACE_HTML_PATH.read_text(encoding="utf-8")
