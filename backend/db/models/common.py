from __future__ import annotations

from sqlalchemy import Enum as SqlEnum


def enum_column(enum_cls: type, name: str) -> SqlEnum:
    return SqlEnum(
        enum_cls,
        name=name,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )
