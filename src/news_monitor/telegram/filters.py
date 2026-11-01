"""Reusable aiogram filters.

Kept in its own module so that every router (moderation and source
administration) can share the authorization filter without importing each other.
"""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject

from news_monitor.config.settings import Settings
from news_monitor.telegram.access import AccessControl


class AdminOnly(BaseFilter):
    """Passes only for numeric ids listed in the administrator settings.

    Works for both messages and callback queries: only ``from_user.id`` is used
    and an event without a user never passes.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return self.settings.is_admin(getattr(user, "id", None))


class CanModerate(BaseFilter):
    """Passes for administrators and for assigned moderators.

    Used only by the moderation router; the source and settings panels stay
    behind :class:`AdminOnly`.
    """

    def __init__(self, access: AccessControl) -> None:
        self.access = access

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return self.access.can_moderate(getattr(user, "id", None))
