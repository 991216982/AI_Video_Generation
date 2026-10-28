"""Development identity for the local, single-user Stage 1 API."""

from __future__ import annotations

import os
from uuid import UUID


_DEFAULT_DEVELOPMENT_OWNER = UUID("00000000-0000-4000-8000-000000000001")


def development_owner_user_id() -> UUID:
    """Return the server-configured local owner; callers cannot override it."""

    configured = os.getenv("BOOK_VIDEO_DEV_OWNER_ID")
    return UUID(configured) if configured else _DEFAULT_DEVELOPMENT_OWNER
