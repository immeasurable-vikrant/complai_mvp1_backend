"""
ComplAI — API Cost Tracker
Tracks cost per API call and accumulates it on the Document record.

Pricing (as of June 2024):
  Claude Haiku:   $0.25 / 1M input tokens,  $1.25 / 1M output tokens
  Claude Sonnet:  $3.00 / 1M input tokens,  $15.00 / 1M output tokens (vision)
  Google DocAI:   $0.065 per page (after free tier of 1000 pages/month)

All costs stored in USD. Displayed in INR in the UI (×84).
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)

# Pricing per token (USD)
PRICING = {
    "claude-haiku": {
        "input_per_token":  0.00000025,   # $0.25 / 1M
        "output_per_token": 0.00000125,   # $1.25 / 1M
    },
    "claude-sonnet-vision": {
        "input_per_token":  0.000003,     # $3.00 / 1M
        "output_per_token": 0.000015,     # $15.00 / 1M
    },
    "google-docai": {
        "per_page": 0.065,                # $0.065 per page
    },
}

# Thread-local accumulator — reset per document processing task
_cost_accumulator = threading.local()


def reset_cost():
    """Call at the start of each document processing task."""
    _cost_accumulator.total_usd  = 0.0
    _cost_accumulator.claude_usd = 0.0
    _cost_accumulator.docai_usd  = 0.0


def get_accumulated_cost() -> float:
    """Return total cost accumulated since last reset_cost() call."""
    return getattr(_cost_accumulator, "total_usd", 0.0)


def get_accumulated_claude_cost() -> float:
    """Return Anthropic Claude cost accumulated since last reset_cost() call."""
    return getattr(_cost_accumulator, "claude_usd", 0.0)


def get_accumulated_docai_cost() -> float:
    """Return Google DocAI cost accumulated since last reset_cost() call."""
    return getattr(_cost_accumulator, "docai_usd", 0.0)


def track_claude_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """
    Calculate and accumulate cost for a Claude API call.
    Returns the cost of this call in USD.
    """
    pricing = PRICING.get(model, PRICING["claude-haiku"])
    cost = (
        input_tokens  * pricing["input_per_token"] +
        output_tokens * pricing["output_per_token"]
    )

    if not hasattr(_cost_accumulator, "total_usd"):
        _cost_accumulator.total_usd  = 0.0
        _cost_accumulator.claude_usd = 0.0
    _cost_accumulator.total_usd  += cost
    _cost_accumulator.claude_usd += cost

    logger.debug(
        f"[cost] {model}: input={input_tokens} output={output_tokens} "
        f"cost=${cost:.6f} total=${_cost_accumulator.total_usd:.6f}"
    )
    return cost


def track_docai_cost(pages: int) -> float:
    """Calculate and accumulate cost for a Google Document AI call."""
    cost = pages * PRICING["google-docai"]["per_page"]

    if not hasattr(_cost_accumulator, "total_usd"):
        _cost_accumulator.total_usd = 0.0
        _cost_accumulator.docai_usd = 0.0
    _cost_accumulator.total_usd += cost
    _cost_accumulator.docai_usd += cost

    logger.debug(f"[cost] docai: pages={pages} cost=${cost:.6f}")
    return cost


def usd_to_inr(usd: float) -> float:
    """Convert USD to INR for display. Rate hardcoded — update periodically."""
    INR_PER_USD = 84.0
    return usd * INR_PER_USD
