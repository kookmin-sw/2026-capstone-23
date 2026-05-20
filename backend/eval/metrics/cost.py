"""Approximate API cost estimation for evaluation output."""

_INPUT_PRICE_PER_1M = 0.40
_OUTPUT_PRICE_PER_1M = 0.16


def compute_cost(engine: str, text: str) -> dict:
    """
    Returns:
        {
          "is_free": bool,
          "estimated_usd": float or None,
          "note": str,
        }
    """
    char_count = len(text)
    estimated_tokens = char_count * 0.4
    estimated_usd = (estimated_tokens / 1_000_000) * (_INPUT_PRICE_PER_1M + _OUTPUT_PRICE_PER_1M)

    return {
        "is_free": False,
        "estimated_usd": round(estimated_usd, 6),
        "note": f"estimated from text length (~{int(estimated_tokens):,} tokens)",
    }
