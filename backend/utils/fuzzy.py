"""
ComplAI — Fuzzy Party Name Matching
Used in bank statement reconciliation to match narration text to known vendors.

Scoring (fuzzywuzzy token_sort_ratio):
  >= 85 → auto-match (match_confirmed stays False until CA confirms)
  70-84 → suggested match, needs_review = True
  < 70  → unmatched, CA maps manually

Token sort ratio is better than simple ratio for Indian party names because:
  "Sharma Trading Co." vs "Co. Sharma Trading" → 100 (order doesn't matter)
  "ABC Pvt Ltd" vs "ABC Private Limited" → ~85 (common abbreviations work)
"""

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Thresholds
AUTO_MATCH_THRESHOLD    = 85
SUGGEST_MATCH_THRESHOLD = 70


def find_best_match(
    narration: str,
    known_names: List[str],
) -> Tuple[Optional[str], int, bool]:
    """
    Find the best matching party name for a bank narration.

    Args:
        narration   — bank statement narration text
        known_names — list of known vendor/client names from invoices table

    Returns:
        (matched_name, score, needs_review)
        Returns (None, 0, True) if no match found above threshold.
    """
    if not narration or not known_names:
        return None, 0, True

    try:
        from fuzzywuzzy import fuzz
    except ImportError:
        logger.warning("[fuzzy] fuzzywuzzy not installed — skipping matching")
        return None, 0, True

    best_name  = None
    best_score = 0

    for name in known_names:
        score = fuzz.token_sort_ratio(narration.lower(), name.lower())
        if score > best_score:
            best_score = score
            best_name  = name

    if best_score >= AUTO_MATCH_THRESHOLD:
        logger.debug(f"[fuzzy] Auto-match '{narration[:30]}' → '{best_name}' ({best_score})")
        return best_name, best_score, False  # high confidence

    elif best_score >= SUGGEST_MATCH_THRESHOLD:
        logger.debug(f"[fuzzy] Suggested match '{narration[:30]}' → '{best_name}' ({best_score})")
        return best_name, best_score, True   # needs CA confirmation

    else:
        logger.debug(f"[fuzzy] No match for '{narration[:30]}' (best={best_score})")
        return None, best_score, True        # unmatched


def get_known_names_for_firm(firm_id: int, client_id: int, db) -> List[str]:
    """
    Collect all vendor/client names from the invoices table.
    These are the names we fuzzy-match bank narrations against.
    CA-confirmed matches from previous runs are included.
    """
    from models.db import Invoice, BankTransaction, Client

    names = set()

    # Vendor names from invoices this firm has processed
    invoices = (
        db.query(Invoice.vendor_name)
        .join(Invoice.document)
        .filter(Invoice.document.has(firm_id=firm_id))
        .filter(Invoice.vendor_name.isnot(None))
        .all()
    )
    for (name,) in invoices:
        if name:
            names.add(name.strip())

    # Previously confirmed ledger matches (human-validated, high quality)
    confirmed_matches = (
        db.query(BankTransaction.matched_ledger)
        .filter(
            BankTransaction.firm_id == firm_id,
            BankTransaction.match_confirmed == True,
            BankTransaction.matched_ledger.isnot(None),
        )
        .all()
    )
    for (name,) in confirmed_matches:
        if name:
            names.add(name.strip())

    return list(names)
