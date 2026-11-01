"""Who may do what: administrators (env) and moderators (database).

Administrators come from ``TELEGRAM_ADMIN_IDS`` and have full rights; the first
one is the owner. Moderators are added at runtime, live in the database and may
only approve or reject cards. The set of moderator ids is cached in memory so
that the per-press authorization check never touches the database, and every
change updates the database first and the cache second.
"""

from __future__ import annotations

import enum
import logging

from news_monitor.config.settings import Settings
from news_monitor.storage.repository import NewsRepository

logger = logging.getLogger(__name__)

#: Upper bound on runtime-assigned moderators.
MAX_MODERATORS = 50


class AddModeratorResult(str, enum.Enum):
    """Outcome of :meth:`AccessControl.add_moderator`."""

    ADDED = "added"
    ALREADY_MODERATOR = "already_moderator"
    IS_ADMIN = "is_admin"
    LIMIT_REACHED = "limit_reached"


class AccessControl:
    """Role lookup shared by the bot filters, the service and the card sender."""

    def __init__(self, settings: Settings, repository: NewsRepository) -> None:
        self._settings = settings
        self._repository = repository
        self._moderators: set[int] = set()

    async def load(self) -> None:
        """Read the moderators from the database (call once at startup)."""
        stored = await self._repository.list_moderators()
        self._moderators = {int(row.user_id) for row in stored}

    @property
    def owner_id(self) -> int | None:
        """The first configured administrator, the default report recipient."""
        admins = self._settings.admin_ids
        return admins[0] if admins else None

    @property
    def moderator_ids(self) -> tuple[int, ...]:
        """Current moderator ids, sorted."""
        return tuple(sorted(self._moderators))

    def is_admin(self, user_id: int | None) -> bool:
        """True for ids listed in TELEGRAM_ADMIN_IDS."""
        return self._settings.is_admin(user_id)

    def is_moderator(self, user_id: int | None) -> bool:
        """True for an assigned moderator who is not an administrator."""
        if user_id is None or self.is_admin(user_id):
            return False
        return user_id in self._moderators

    def can_moderate(self, user_id: int | None) -> bool:
        """True for administrators and moderators."""
        return self.is_admin(user_id) or self.is_moderator(user_id)

    async def add_moderator(
        self, user_id: int, display_name: str = "", added_by: int | None = None
    ) -> AddModeratorResult:
        """Assign a moderator; an administrator can never become one."""
        if self.is_admin(user_id):
            return AddModeratorResult.IS_ADMIN
        if user_id in self._moderators:
            return AddModeratorResult.ALREADY_MODERATOR
        if len(self._moderators) >= MAX_MODERATORS:
            return AddModeratorResult.LIMIT_REACHED
        added = await self._repository.add_moderator(user_id, display_name, added_by)
        self._moderators.add(user_id)
        if not added:
            return AddModeratorResult.ALREADY_MODERATOR
        logger.info("moderator added (now %s)", len(self._moderators))
        return AddModeratorResult.ADDED

    async def remove_moderator(self, user_id: int) -> bool:
        """Revoke the role at once. Returns False when the user was not one."""
        removed = await self._repository.remove_moderator(user_id)
        self._moderators.discard(user_id)
        if removed:
            logger.info("moderator removed (now %s)", len(self._moderators))
        return removed
