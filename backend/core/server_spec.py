"""
서버 사양 점검: CPU·메모리 기반 안정적 병렬 처리 수 제안
"""
import os
from typing import Tuple, Optional


def get_cpu_count() -> int:
    """논리 CPU 코어 수 (None이면 4 가정)."""
    n = os.cpu_count()
    return n if n is not None and n > 0 else 4


def get_ram_gb() -> Optional[float]:
    """사용 가능한 RAM(GB). Linux는 MemTotal 기준, 실패 시 None."""
    try:
        if os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        parts = line.split()
                        # kB 단위
                        if len(parts) >= 3 and parts[2].upper() == "KB":
                            kb = int(parts[1])
                            return round(kb / (1024 * 1024), 2)
                        break
    except Exception:
        pass
    return None


def suggest_max_parallel() -> Tuple[int, str, str]:
    """
    서버 사양에 따른 안정적인 병렬 처리 수 제안.

    Returns:
        (max_workers, reason, speed_prediction)
    """
    cpu = get_cpu_count()
    ram_gb = get_ram_gb()

    # 작업당 대략 2GB 가정 (VLM+PDF/이미지 처리)
    per_task_gb = 2.0
    if ram_gb is not None:
        by_ram = max(1, int(ram_gb / per_task_gb))
    else:
        by_ram = None

    # I/O·API 대기 비중이 크므로 CPU의 1.5~2배까지는 무리 없음; 상한 16
    by_cpu = min(cpu * 2, 16) if cpu else 4
    by_cpu = max(1, by_cpu)

    if by_ram is not None:
        max_workers = min(by_cpu, by_ram, 16)
        max_workers = max(1, max_workers)
        reason = f"CPU {cpu}코어, RAM {ram_gb}GB 기준 — 작업당 약 {per_task_gb}GB 가정, RAM·CPU 상한 적용."
    else:
        max_workers = min(by_cpu, 8)
        max_workers = max(1, max_workers)
        reason = f"CPU {cpu}코어 기준 (RAM 미감지). 보수적으로 최대 {max_workers} 권장."

    # 속도 예측 문구
    if max_workers <= 1:
        speed_prediction = "병렬 1이면 현재와 동일한 처리량입니다."
    else:
        # I/O 바운드 비율 가정 시 이론상 선형에 가깝지만, API/디스크 경합으로 0.7~0.9 수준
        effective = min(max_workers, 4) * 0.85 if max_workers <= 4 else 4 * 0.85 + (max_workers - 4) * 0.6
        speed_prediction = (
            f"병렬 {max_workers} 사용 시 이론상 최대 약 {max_workers}배까지 처리량 향상 가능. "
            f"실제로는 API·디스크 대기 비중에 따라 약 {effective:.1f}~{max_workers}배 구간을 예상할 수 있습니다."
        )

    return max_workers, reason, speed_prediction
