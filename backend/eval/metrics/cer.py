"""CER (Character Error Rate) — 텍스트 추출 정확도"""


def _edit_distance(a: str, b: str) -> int:
    """레벤슈타인 거리 계산"""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = curr
    return prev[-1]


def compute_cer(hypothesis: str, reference: str) -> float:
    """
    CER = edit_distance(hypothesis, reference) / len(reference)
    0.0 = 완벽, 1.0 = 전혀 다름 (1 초과 가능)
    GT(reference)가 없으면 None 반환
    """
    if not reference:
        return None
    # 공백 정규화
    hyp = " ".join(hypothesis.split())
    ref = " ".join(reference.split())
    dist = _edit_distance(hyp, ref)
    return dist / max(len(ref), 1)
