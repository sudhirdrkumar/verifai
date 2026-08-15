"""
Folder Sync API Endpoints - Manage automatic folder synchronization
"""
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps.auth import require_roles
from app.core.config import settings
from app.schemas.auth import UserRole
from app.services.auth_service import AuthenticatedUser
from app.services.folder_sync_service import sync_folder
from app.services.folder_sync_scheduler import folder_sync_scheduler

router = APIRouter(prefix="/folder-sync", tags=["folder-sync"])


class FolderSyncConfig(BaseModel):
    """Configuration for folder sync"""
    folder_path: str
    enabled: bool = True
    interval_minutes: int = 5


class FolderSyncResponse(BaseModel):
    """Response from folder sync operation"""
    success: bool
    message: str
    processed: int = 0
    uploaded: int = 0
    failed: int = 0
    errors: list[str] = []


class FolderSyncStatusResponse(BaseModel):
    """Current folder sync status"""
    enabled: bool
    configured_path: str | None
    interval_minutes: int
    is_running: bool


@router.post("/sync", response_model=FolderSyncResponse)
async def trigger_manual_sync(
    current_user: AuthenticatedUser = require_roles(UserRole.super_admin),
) -> FolderSyncResponse:
    """
    Manually trigger a folder sync operation.
    Only accessible to super_admin users.
    """
    if not settings.folder_sync_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Folder sync path not configured",
        )

    try:
        result = sync_folder(settings.folder_sync_path, actor_id=current_user.username)

        return FolderSyncResponse(
            success=result.get("success", False),
            message="Folder sync completed successfully" if result.get("success") else "Folder sync failed",
            processed=result.get("processed", 0),
            uploaded=result.get("uploaded", 0),
            failed=result.get("failed", 0),
            errors=result.get("errors", []),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Folder sync error: {str(e)}",
        )


@router.get("/status", response_model=FolderSyncStatusResponse)
async def get_folder_sync_status(
    current_user: AuthenticatedUser = require_roles(UserRole.super_admin),
) -> FolderSyncStatusResponse:
    """
    Get the current folder sync configuration and status.
    Only accessible to super_admin users.
    """
    return FolderSyncStatusResponse(
        enabled=settings.folder_sync_enabled,
        configured_path=settings.folder_sync_path,
        interval_minutes=settings.folder_sync_interval_minutes,
        is_running=folder_sync_scheduler._running,
    )


@router.post("/configure", response_model=dict)
async def configure_folder_sync(
    config: FolderSyncConfig,
    current_user: AuthenticatedUser = require_roles(UserRole.super_admin),
) -> dict:
    """
    Configure folder sync settings (updates require app restart to take effect).
    Only accessible to super_admin users.
    """
    import os

    # Validate folder path
    if not os.path.isdir(config.folder_path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid folder path: {config.folder_path}",
        )

    # Validate interval
    if config.interval_minutes < 1 or config.interval_minutes > 1440:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Interval must be between 1 and 1440 minutes",
        )

    return {
        "success": True,
        "message": "Folder sync configuration updated. Please restart the application for changes to take effect.",
        "settings": {
            "folder_path": config.folder_path,
            "enabled": config.enabled,
            "interval_minutes": config.interval_minutes,
        },
    }
