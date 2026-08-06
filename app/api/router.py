from fastapi import APIRouter

from app.api.v1.endpoints.admin_tools import router as admin_tools_router
from app.api.v1.endpoints.auth import router as auth_router
from app.api.v1.endpoints.checklist import router as checklist_router
from app.api.v1.endpoints.claims import router as claims_router
from app.api.v1.endpoints.documents import router as documents_router
from app.api.v1.endpoints.extractions import router as extractions_router
from app.api.v1.endpoints.health import router as health_router
from app.api.v1.endpoints.integrations import router as integrations_router
from app.api.v1.endpoints.user_tools import router as user_tools_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(integrations_router)
api_router.include_router(auth_router)
api_router.include_router(admin_tools_router)
api_router.include_router(claims_router)
api_router.include_router(documents_router)
api_router.include_router(extractions_router)
api_router.include_router(checklist_router)
api_router.include_router(user_tools_router)
