"""Config endpoints: application branding and the current user's identity."""

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..services.user import get_current_user, get_workspace_url

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get('/config/app')
async def get_app_config() -> dict[str, Any]:
    """Return the application configuration used for branding.

    :return: ``app_name`` and a ``branding`` dict (name, logo path, company name).
    """
    return {
        'app_name': 'LatLang',
        'branding': {
            'name': 'LatLang',
            'logo': '/logos/LOGO_LATECOERE.png',
            'company_name': 'Powered by Databricks',
        },
    }


@router.get('/me')
async def get_me(request: Request) -> Any:
    """Return the current user's identity and the workspace URL.

    :param request: Incoming request carrying the forwarded user header.
    :return: ``{'user', 'workspace_url'}``, or a 500 JSON response with ``error`` when the identity cannot be resolved.
    """
    try:
        user = await get_current_user(request)
        return {'user': user, 'workspace_url': get_workspace_url()}
    except Exception as e:
        logger.error('Error getting user info: %s', e)
        return JSONResponse({'error': str(e)}, status_code=500)
