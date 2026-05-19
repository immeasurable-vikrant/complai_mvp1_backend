"""
ComplAI — Invoice Validator (LangGraph Node 4)
Runs 5 validation checks on each extracted invoice:
  1. GSTIN format validation
  2. Duplicate invoice detection (SHA-256 hash)
  3. Total crosscheck (taxable + GST ≈ invoice_total)
  4. Date sanity (not in the future)
  5. Missing critical fields check
"""

import hashlib
import logging
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# GSTIN regex: 2 digit state code + 5 alpha PAN chars + 4 digit PAN + 1 alpha + 1 alphanumeric + Z + 1 alphanumeric
GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")

# Tolerance for total mismatch (₹2)
TOTAL_MISMATCH_TOLERANCE = 2.0


def validate_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    LangGraph Node 4: validate_node
    Validates all extracted invoices and appends issues list to each.
    Requires DB session for duplicate check — passed via state.
    """
    invoices = state.get("extracted_invoices", [])
    db_session = state.get("db_session")  # injected by Celery task

    validated = []
    for inv in invoices:
        issues = list(inv.get("issues") or [])  # preserve any issues Claude added
        issues.extend(_run_validations(inv, db_session))
        inv["issues"] = issues
        validated.append(inv)

    state["extracted_invoices"] = validated
    return state


def _run_validations(inv: Dict[str, Any], db_session=None) -> List[str]:
    """Run all validation checks. Returns list of issue strings."""
    issues = []

    # 1. GSTIN format
    issues.extend(_validate_gstin(inv.get("vendor_gstin"), "vendor"))
    issues.extend(_validate_gstin(inv.get("buyer_gstin"),  "buyer"))

    # 2. Duplicate check
    if db_session:
        dup_issue = _check_duplicate(inv, db_session)
        if dup_issue:
            issues.append(dup_issue)

    # 3. Total crosscheck
    issues.extend(_validate_totals(inv))

    # 4. Date validation
    issues.extend(_validate_date(inv.get("invoice_date")))

    # 5. Missing critical fields
    issues.extend(_check_missing_fields(inv))

    return issues


def _validate_gstin(gstin: Optional[str], party: str) -> List[str]:
    """Check GSTIN format. Returns list with one issue string if invalid."""
    if not gstin:
        return []  # null GSTIN is caught by _check_missing_fields if critical
    clean = gstin.strip().upper()
    if not GSTIN_PATTERN.match(clean):
        return [f"Invalid {party} GSTIN: {gstin}"]
    return []


def _check_duplicate(inv: Dict[str, Any], db_session) -> Optional[str]:
    """
    SHA-256 hash of (invoice_number + vendor_gstin + invoice_total).
    If the same hash exists in the DB, it's a duplicate submission.
    """
    from models.db import Invoice

    invoice_number = str(inv.get("invoice_number") or "")
    vendor_gstin   = str(inv.get("vendor_gstin")   or "")
    invoice_total  = str(inv.get("invoice_total")  or "")

    if not invoice_number:
        return None  # can't compute reliable hash without invoice number

    hash_str  = f"{invoice_number}_{vendor_gstin}_{invoice_total}"
    dedup_hash = hashlib.sha256(hash_str.encode()).hexdigest()

    existing = db_session.query(Invoice).filter(Invoice.dedup_hash == dedup_hash).first()
    if existing:
        return f"Duplicate invoice: already exists as invoice ID {existing.id}"

    # Store hash in inv for later DB save
    inv["_dedup_hash"] = dedup_hash
    return None


def _validate_totals(inv: Dict[str, Any]) -> List[str]:
    """Check that taxable_value + total_gst ≈ invoice_total."""
    taxable = inv.get("taxable_value")
    total_gst = inv.get("total_gst")
    invoice_total = inv.get("invoice_total")

    if taxable is None or total_gst is None or invoice_total is None:
        return []  # can't check without all three

    try:
        expected = float(taxable) + float(total_gst)
        actual   = float(invoice_total)
        if abs(expected - actual) > TOTAL_MISMATCH_TOLERANCE:
            return [f"Total mismatch: {expected:.2f} ≠ {actual:.2f}"]
    except (TypeError, ValueError):
        return ["Could not validate totals — non-numeric values"]

    return []


def _validate_date(invoice_date: Any) -> List[str]:
    """Flag invoices dated in the future."""
    if invoice_date is None:
        return []

    try:
        if isinstance(invoice_date, str):
            # Try DD/MM/YYYY (common Indian format)
            for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
                try:
                    parsed = datetime.strptime(invoice_date, fmt).date()
                    break
                except ValueError:
                    continue
            else:
                return [f"Unrecognised date format: {invoice_date}"]
        elif isinstance(invoice_date, date):
            parsed = invoice_date
        else:
            return []

        if parsed > date.today():
            return [f"Future date: {parsed.strftime('%d/%m/%Y')}"]

    except Exception as e:
        logger.warning(f"[validate] Date check error: {e}")

    return []


def _check_missing_fields(inv: Dict[str, Any]) -> List[str]:
    """Report absence of the three most critical invoice identity fields."""
    issues = []
    if not inv.get("invoice_number"):
        issues.append("Missing invoice number")
    if not inv.get("invoice_date"):
        issues.append("Missing invoice date")
    if not inv.get("vendor_name"):
        issues.append("Missing vendor name")
    return issues
