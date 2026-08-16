"""Time-ordered UUIDv7 identifiers (plan section 6: UUIDv7 primary keys)."""

from uuid import UUID

import uuid_utils


def new_uuid7() -> UUID:
    """Time-ordered UUIDv7 as a stdlib UUID (sortable, index-friendly)."""
    return UUID(str(uuid_utils.uuid7()))
