import os
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


@lru_cache(maxsize=1)
def load_project_env() -> tuple[str, ...]:
    loaded_paths: list[str] = []
    original_keys = set(os.environ.keys())
    loaded_values: dict[str, str] = {}
    candidates = (
        PROJECT_ROOT / ".env",
        PROJECT_ROOT / ".env.local",
    )

    for candidate in candidates:
        if not candidate.exists():
            continue

        values = dotenv_values(candidate)
        for key, value in values.items():
            if value is None:
                continue
            loaded_values[key] = value
        loaded_paths.append(str(candidate))

    for key, value in loaded_values.items():
        if key in original_keys:
            continue
        os.environ[key] = value

    return tuple(loaded_paths)


def env_raw(name: str, default: str | None = None) -> str | None:
    load_project_env()
    return os.environ.get(name, default)


def project_environ() -> dict[str, str]:
    load_project_env()
    return dict(os.environ)


def env_str(name: str, default: str = "", *, strip: bool = False) -> str:
    value = env_raw(name)
    if value is None:
        return default
    return value.strip() if strip else value


def env_bool(name: str, default: bool = False) -> bool:
    value = env_raw(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = env_raw(name)
    if value is None or value.strip() == "":
        result = int(default)
    else:
        try:
            result = int(value.strip())
        except ValueError:
            result = int(default)
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def env_first_int(
    names: Sequence[str],
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    for name in names:
        value = env_raw(name)
        if value is None or value.strip() == "":
            continue
        try:
            result = int(value.strip())
        except ValueError:
            continue
        if minimum is not None:
            result = max(minimum, result)
        if maximum is not None:
            result = min(maximum, result)
        return result
    result = int(default)
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = env_raw(name)
    if value is None or value.strip() == "":
        result = float(default)
    else:
        try:
            result = float(value.strip())
        except ValueError:
            result = float(default)
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def env_csv(name: str, default: str = "") -> set[str]:
    raw = env_str(name, default)
    return {item.strip() for item in raw.split(",") if item.strip()}


def env_path(name: str, default: str | Path, *, base: Path = PROJECT_ROOT) -> Path:
    value = env_raw(name)
    raw_path = str(default) if value is None or value.strip() == "" else value.strip()
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()
