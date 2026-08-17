"""Fail-closed storage-capacity checks for scientific execution preflights."""

from __future__ import annotations

from pathlib import Path
import math
import shutil
from typing import Callable, NamedTuple


BYTES_PER_GIB = 1024**3


class StorageCapacityError(RuntimeError):
    """Raised when immediately allocatable space is below the declared policy."""

    code = "FAILED_STORAGE_CAPACITY"


class DiskUsage(NamedTuple):
    total: int
    used: int
    free: int


def check_storage_capacity(
    path: Path | str,
    *,
    minimum_free_gb: float,
    usage_provider: Callable[[Path], DiskUsage] = shutil.disk_usage,
) -> dict[str, object]:
    """Measure the target filesystem and fail closed below the configured floor."""

    if (
        not isinstance(minimum_free_gb, (int, float))
        or isinstance(minimum_free_gb, bool)
        or not math.isfinite(float(minimum_free_gb))
        or float(minimum_free_gb) <= 0
    ):
        raise ValueError("minimum_free_gb must be a finite positive number")
    selected = Path(path).expanduser()
    if not selected.is_absolute():
        raise StorageCapacityError(
            f"{StorageCapacityError.code}: storage check path must be absolute"
        )
    selected = selected.resolve()
    if not selected.is_dir():
        raise StorageCapacityError(
            f"{StorageCapacityError.code}: storage check path is missing: {selected}"
        )
    usage = usage_provider(selected)
    required_bytes = math.ceil(float(minimum_free_gb) * BYTES_PER_GIB)
    result: dict[str, object] = {
        "status": "passed" if usage.free >= required_bytes else "failed",
        "check_path": str(selected),
        "measurement": "immediately_allocatable_filesystem_bytes",
        "free_bytes": usage.free,
        "free_gib": usage.free / BYTES_PER_GIB,
        "minimum_free_gib": float(minimum_free_gb),
        "required_bytes": required_bytes,
    }
    if usage.free < required_bytes:
        raise StorageCapacityError(
            f"{StorageCapacityError.code}: {result['free_gib']:.2f} GiB free at "
            f"{selected}; at least {minimum_free_gb:.2f} GiB is required"
        )
    return result


__all__ = ["BYTES_PER_GIB", "StorageCapacityError", "check_storage_capacity"]
