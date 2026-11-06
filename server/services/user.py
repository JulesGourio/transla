"""User service — resolve the current user's identity from request headers.

Access is gated at the Databricks App level (workspace group permissions on
the app resource), not per feature in application code, so there is no
capability matrix. `require_translate` accepts every request.
"""

import asyncio
import logging
import os
from typing import Optional, TypedDict

from fastapi import Request


class UserIdentity(TypedDict):
    user_id: str                 # Numeric part only — e.g. "78655966095635"
    workspace_id: Optional[str]  # Workspace code (part after '@') — e.g. "2865348338307293"
    email: Optional[str]         # Real email if x-forwarded-user is one, otherwise None


logger = logging.getLogger(__name__)

_dev_user_cache: Optional[str] = None


def _is_dev() -> bool:
    """Tell whether the app runs in local development.

    :return: True when the ENV variable is ``development`` (the default).
    """
    return os.getenv('ENV', 'development') == 'development'


def _is_real_email(value: str) -> bool:
    """Tell whether a value is a real email rather than a numeric Databricks workspace-scoped ID.

    :param value: Raw ``x-forwarded-user`` header value.
    :return: True when the value has an ``@`` and is not ``<digits>@<digits>``.
    """
    if '@' not in value:
        return False
    local, _, domain = value.partition('@')
    return not (local.isdigit() and domain.isdigit())


def get_workspace_url() -> str:
    """Return the Databricks workspace base URL.

    :return: ``https://host`` without trailing slash, or an empty string when the host cannot be resolved.
    """
    host = os.getenv('DATABRICKS_HOST', '')
    if not host:
        try:
            from databricks.sdk import WorkspaceClient
            host = WorkspaceClient().config.host or ''
        except Exception:
            pass
    if not host:
        return ''
    host = host.rstrip('/')
    if not host.startswith('http'):
        host = f'https://{host}'
    return host


async def get_current_user(request: Request) -> str:
    """Return the authenticated user's email, or the numeric part of their Databricks ID.

    Falls back to the SDK's current user in local development only.

    :param request: Incoming request carrying the ``x-forwarded-user`` header.
    :return: The user's email or numeric user id.
    :raises ValueError: When no identity can be found outside development.
    """
    forwarded = request.headers.get('x-forwarded-user', '').strip()
    if forwarded and _is_real_email(forwarded):
        return forwarded
    if forwarded:
        return forwarded.split('@', 1)[0]
    if _is_dev():
        return await _dev_user()
    raise ValueError('No user identity found in request headers.')


async def get_user_identity(request: Request) -> UserIdentity:
    """Split the forwarded user header into user id, workspace id and email.

    :param request: Incoming request carrying the ``x-forwarded-user`` header.
    :return: The identity; ``user_id`` is empty when no header is present outside development.
    """
    forwarded = request.headers.get('x-forwarded-user', '').strip()

    if not forwarded:
        if _is_dev():
            user = await _dev_user()
            return UserIdentity(user_id=user, workspace_id=None, email=user)
        logger.warning('x-forwarded-user header is absent — cannot identify user')
        return UserIdentity(user_id='', workspace_id=None, email=None)

    if _is_real_email(forwarded):
        return UserIdentity(user_id=forwarded, workspace_id=None, email=forwarded)

    parts = forwarded.split('@', 1)
    clean_uid = parts[0]
    workspace_id: Optional[str] = parts[1] if len(parts) > 1 else None
    return UserIdentity(user_id=clean_uid, workspace_id=workspace_id, email=None)


async def require_translate(request: Request) -> None:
    """Accept every request; access is gated at the Databricks App level.

    :param request: Incoming request.
    """


async def _dev_user() -> str:
    """Resolve and cache the SDK's current user name for local development.

    :return: The user name, or the display name when absent.
    :raises ValueError: When the SDK cannot determine the user.
    """
    global _dev_user_cache
    if _dev_user_cache:
        return _dev_user_cache
    from databricks.sdk import WorkspaceClient
    try:
        me = await asyncio.to_thread(lambda: WorkspaceClient().current_user.me())
        _dev_user_cache = me.user_name or me.display_name or ''
    except Exception as e:
        raise ValueError(f'Could not determine current user: {e}') from e
    return _dev_user_cache
