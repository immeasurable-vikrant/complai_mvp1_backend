"""
ComplAI — Confidence Score Calculator
Combined confidence = OCR confidence × Claude confidence.

This multiplicative model means both sources must be high for auto-acceptance.
Example: OCR=0.90, Claude=0.85 → combined=0.765 (amber, review recommended)
         OCR=0.99, Claude=0.90 → combined=0.891 (green, auto-accepted)
"""


def compute_combined_confidence(ocr_confidence: float, claude_confidence: float) -> float:
    """
    Compute combined confidence as geometric product.
    Both inputs expected in [0.0, 1.0].
    Output is clamped to [0.0, 1.0].
    """
    combined = float(ocr_confidence) * float(claude_confidence)
    return max(0.0, min(1.0, combined))


def confidence_label(combined: float) -> str:
    """
    Human-readable label for confidence level.
    Used in frontend color coding and UI badges.
    """
    if combined >= 0.80:
        return "high"      # green — auto accepted
    elif combined >= 0.60:
        return "medium"    # amber — review recommended
    else:
        return "low"       # red   — must review


def confidence_color(combined: float) -> str:
    """Return CSS color class name for the frontend."""
    label = confidence_label(combined)
    return {"high": "green", "medium": "amber", "low": "red"}[label]
