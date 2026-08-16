"""Vocabulário do domínio de identidade e acesso."""
from __future__ import annotations

from typing import Final

USER_TYPE_ADMINISTRATOR: Final = "administrator"
USER_TYPE_SUPER_USER: Final = "super user"
USER_TYPE_USER: Final = "user"
VALID_USER_TYPES: Final = (
    USER_TYPE_ADMINISTRATOR,
    USER_TYPE_SUPER_USER,
    USER_TYPE_USER,
)
USER_TYPE_LABELS: Final = {
    USER_TYPE_ADMINISTRATOR: USER_TYPE_ADMINISTRATOR,
    USER_TYPE_SUPER_USER: USER_TYPE_SUPER_USER,
    USER_TYPE_USER: USER_TYPE_USER,
}


def normalize_user_type(value: str | None) -> str:
    if value is None:
        return USER_TYPE_ADMINISTRATOR
    normalized = value.strip()
    return normalized if normalized in VALID_USER_TYPES else USER_TYPE_ADMINISTRATOR
