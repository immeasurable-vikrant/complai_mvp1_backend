"""
ComplAI — Claude Vision fallback
Standalone wrapper used by ocr.py when Document AI confidence is too low.
Also provides a utility to extract text from any image using Claude Sonnet.
(The main logic lives in ocr.py Layer 3 — this file is the standalone export.)
"""

# Re-export from ocr.py for clean imports
from agents.ocr import _layer3_claude_vision as extract_with_vision

__all__ = ["extract_with_vision"]
