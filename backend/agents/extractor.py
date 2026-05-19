"""
ComplAI — Claude Haiku Extractor (LangGraph Node 3)
Sends OCR text to Claude Haiku for structured JSON extraction.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import anthropic

from utils.cost_tracker import track_claude_cost

logger = logging.getLogger(__name__)

CONFIDENCE_TARGET      = float(os.getenv("CONFIDENCE_TARGET", "0.75"))
EXTRACTION_RETRY_LIMIT = int(os.getenv("EXTRACTION_RETRY_LIMIT", "3"))

SYSTEM_PROMPT = """You are an expert Indian GST invoice parser with deep knowledge of Indian tax law.
Extract structured data from the OCR text below.
Return ONLY valid JSON — no markdown, no explanation.

INPUT FORMAT:
- The text may contain one or more pages separated by "--- PAGE BREAK ---"
- Treat the entire text as one document (a multi-page invoice is still ONE invoice)
- If multiple distinct invoices appear (different invoice numbers / vendors), extract each separately
- OCR may have errors: "O" vs "0", "I" vs "1", "l" vs "1" — use context to correct obvious OCR mistakes

RULES:
1. Back-calculation: if invoice_total given but CGST/SGST/IGST missing:
   - Try 18% GST: taxable = total/1.18, gst = total - taxable (split equally for intrastate)
   - Try 12% GST: taxable = total/1.12
   - Try 5%  GST: taxable = total/1.05
   - Use whichever is mathematically consistent with the visible amounts
   - Set back_calculated=true, explain in back_calc_note
2. GSTIN format: exactly 15 chars — 2-digit state code + 10-char PAN + 1-alpha + Z + 1-alphanumeric
   - Common state codes: 27=Maharashtra, 29=Karnataka, 07=Delhi, 33=Tamil Nadu, 36=Telangana
   - Fix obvious OCR errors (e.g., "27AABCU96380lZF" → "27AABCU96380IZF", "O" → "0" in numeric positions)
   - If you're 70%+ sure of the GSTIN after correction, include it
3. transaction_type: compare first 2 digits of vendor vs buyer GSTIN
   - Same state code → intrastate (CGST+SGST)
   - Different → interstate (IGST)
   - If only one GSTIN available, infer from presence of CGST+SGST (intrastate) or IGST (interstate)
4. Set null for genuinely unclear fields — never guess monetary amounts
5. confidence_score: your overall extraction confidence 0.0–1.0
6. field_confidence: per-field confidence dict, keys are field names, values 0.0–1.0
7. line_items: extract ALL line items. Each must have:
   - description, hsn_sac (HSN for goods / SAC for services, as string), quantity, unit,
     unit_price, taxable_value, gst_rate (%), cgst_amount, sgst_amount, igst_amount
   - line_confidence: confidence for this line item 0.0–1.0
   - If a line item is partially readable, still extract what you can — don't skip it
8. bank_details: extract vendor's bank account info if printed on invoice:
   - account_number, ifsc_code, bank_name, branch, account_type (current/savings)
9. grand_total: extract explicitly if shown separately from invoice_total
10. Indian date formats: DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY — always output as DD/MM/YYYY
11. Amount parsing — Indian invoices use special notation:
    - Indian numbering: "1,23,456.78" = 123456.78 (remove commas)
    - Paise separator: "36=90" = 36.90, "483=80" = 483.80, "300-89" = 300.89
      (shopkeepers use = or - as a decimal point for paise)
    - "Only" suffix: "410=", "600-", "60/", "300/-", "300-/" all mean round rupees
      (the trailing symbol means no paise — "410=" = 410.00, "600-" = 600.00)
    - Always output amounts as plain numbers: 36.90 not "36=90"
    - If a number like "91.36=90" appears: "91" is likely a row/reference number,
      "36=90" is the amount → extract 36.90
12. Digit accuracy — verify every amount is internally consistent BEFORE returning:
    - CGST + SGST = total_gst  (intrastate)  OR  IGST = total_gst  (interstate)
    - taxable_value + total_gst = invoice_total
    - sum of line_item taxable_values ≈ header taxable_value
    - If any check fails, re-read each digit in the offending amounts carefully.
      Common OCR confusions: 4↔9, 1↔7, 6↔0, 8↔3.
      Digit transpositions also occur (e.g. "4823" may be "4832" in the original).
      Prefer the reading that makes the arithmetic consistent.
    - GSTINs: the 15th character is a checksum — if your reading fails the
      checksum, you likely misread one character; try common swaps (4↔9, I↔1, O↔0).

Return this exact JSON structure:
{
  "invoices": [
    {
      "vendor_name": null,
      "vendor_gstin": null,
      "vendor_address": null,
      "buyer_name": null,
      "buyer_gstin": null,
      "buyer_address": null,
      "invoice_number": null,
      "invoice_date": "DD/MM/YYYY or null",
      "place_of_supply": null,
      "line_items": [
        {
          "description": "string",
          "hsn_sac": "string or null",
          "quantity": 0.0,
          "unit": "Nos",
          "unit_price": 0.0,
          "taxable_value": 0.0,
          "gst_rate": 18,
          "cgst_amount": 0.0,
          "sgst_amount": 0.0,
          "igst_amount": null,
          "line_confidence": 0.9
        }
      ],
      "taxable_value": null,
      "cgst": null,
      "sgst": null,
      "igst": null,
      "total_gst": null,
      "invoice_total": null,
      "grand_total": null,
      "transaction_type": "intrastate",
      "bank_details": {
        "account_number": null,
        "ifsc_code": null,
        "bank_name": null,
        "branch": null,
        "account_type": null
      },
      "back_calculated": false,
      "back_calc_note": null,
      "confidence_score": 0.85,
      "field_confidence": {
        "vendor_name": 0.95,
        "vendor_gstin": 0.90,
        "vendor_address": 0.85,
        "buyer_name": 0.90,
        "buyer_gstin": 0.85,
        "buyer_address": 0.80,
        "invoice_number": 0.95,
        "invoice_date": 0.95,
        "place_of_supply": 0.85,
        "transaction_type": 0.90,
        "taxable_value": 0.90,
        "cgst": 0.85,
        "sgst": 0.85,
        "igst": null,
        "total_gst": 0.90,
        "invoice_total": 0.95,
        "grand_total": 0.90
      },
      "issues": []
    }
  ]
}"""

USER_PROMPT_TEMPLATE = "Extract all invoice data from this text:\n\n{ocr_text}"


def extract_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    LangGraph Node 3: extract_node with per-chunk retry logic.

    Strategy depends on doc_type:
    - digital_pdf : extract each page/chunk independently (may be separate invoices)
    - scanned_pdf : combine ALL pages into ONE extraction call so Claude can see
                    the full multi-page invoice rather than partial per-page fragments
    - image       : single chunk, standard extraction
    - excel       : single chunk, standard extraction
    """
    chunks   = state.get("chunks", [])
    doc_type = state.get("doc_type", "digital_pdf")
    all_invoices: List[Dict] = []

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # ── Scanned PDFs: smart combine vs per-page ──────────────────────────────
    # Heuristic:
    #   ≤ 4 pages  → combine into one call (likely a single multi-page invoice)
    #   > 4 pages  → per-page (CA firm batch scan — each page is its own invoice)
    #
    # Why: combining 20 pages of different invoices into one prompt completely
    # confuses Claude Haiku and produces 0 results. Per-page is safer for
    # large batch scans even if it means a 3-page invoice gets split.
    if doc_type == "scanned_pdf" and len(chunks) > 1:
        non_empty = [c for c in chunks if c.get("text", "").strip()]
        if not non_empty:
            logger.warning("[extract] scanned_pdf: all chunks have empty OCR text — nothing to extract")
            state["extracted_invoices"] = []
            return state

        # Combine only when it's genuinely a single multi-page invoice (≤ 2 pages).
        # > 2 pages almost always means a batch scan with one invoice per page.
        # The old threshold of 4 caused batches of 5 to be treated as 3 (after
        # 2 OCR-empty pages dropped out) and combined — Claude then merged them.
        COMBINE_THRESHOLD = 2
        use_combined = len(non_empty) <= COMBINE_THRESHOLD

        if use_combined:
            combined_text = "\n\n--- PAGE BREAK ---\n\n".join(
                f"[Page {c.get('page_range', i + 1)}]\n{c['text']}"
                for i, c in enumerate(non_empty)
            )
            avg_confidence = sum(c.get("confidence", 0.5) for c in non_empty) / len(non_empty)
            layer_used     = non_empty[0].get("layer_used", "claude_vision")
            page_range     = f"1-{len(non_empty)}"

            logger.info(
                f"[extract] scanned_pdf: COMBINED mode — {len(non_empty)} pages, "
                f"{len(combined_text)} chars"
            )
            chunks_to_process = [(combined_text, avg_confidence, page_range, layer_used)]
        else:
            logger.info(
                f"[extract] scanned_pdf: PER-PAGE mode — {len(non_empty)} pages "
                f"(> {COMBINE_THRESHOLD} page threshold, treating as batch scan)"
            )
            chunks_to_process = [
                (c["text"], c.get("confidence", 0.5), c.get("page_range", str(i+1)), c.get("layer_used", "claude_vision"))
                for i, c in enumerate(non_empty)
            ]

        for (text, conf, pg, layer) in chunks_to_process:
            # Empty OCR → create a placeholder so CA knows this page was missed
            if not text.strip():
                logger.warning(f"[extract] page {pg}: empty OCR text — creating placeholder for manual review")
                all_invoices.append(_make_placeholder(pg, layer, conf, reason="ocr_empty"))
                continue

            best_invoices: List[Dict] = []
            best_combined_conf = 0.0
            prev_combined_conf = -1.0   # track previous attempt for stagnation check
            attempt = 1

            _extract_from_text._last_math_issues = []   # reset before each page
            for attempt in range(1, EXTRACTION_RETRY_LIMIT + 1):
                invoices = _extract_from_text(client, text, conf, pg, layer, attempt)
                if invoices:
                    # Collect math issues from this attempt so next retry prompt is specific
                    all_math_issues = []
                    for inv in invoices:
                        all_math_issues.extend(inv.get("_math_issues") or [])
                    _extract_from_text._last_math_issues = all_math_issues

                    attempt_combined = max(
                        (float(inv.get("_ocr_confidence", 0.5)) + float(inv.get("confidence_score", 0.5))) / 2
                        for inv in invoices
                    )
                    if attempt_combined > best_combined_conf:
                        best_combined_conf = attempt_combined
                        best_invoices = invoices
                    if best_combined_conf >= CONFIDENCE_TARGET and not all_math_issues:
                        logger.info(f"[extract] page {pg} hit confidence target: {best_combined_conf:.2f}")
                        break
                    # Stop retrying early if confidence is not improving between attempts
                    if attempt > 1 and abs(attempt_combined - prev_combined_conf) < 0.02 and not all_math_issues:
                        logger.info(f"[extract] page {pg} confidence stagnant at {attempt_combined:.2f} — stopping retries early")
                        break
                    prev_combined_conf = attempt_combined
                    if all_math_issues:
                        logger.info(f"[extract] page {pg} attempt {attempt}: math failed — will retry with specific error context")
                else:
                    logger.warning(f"[extract] page {pg} attempt {attempt} returned empty — retrying")

                if attempt < EXTRACTION_RETRY_LIMIT:
                    logger.info(f"[extract] page {pg} attempt {attempt} conf={best_combined_conf:.2f} retrying...")

            if best_invoices:
                for inv in best_invoices:
                    inv["_retry_count"] = attempt - 1
                all_invoices.extend(best_invoices)
                logger.info(f"[extract] page {pg}: extracted {len(best_invoices)} invoice(s)")
            else:
                # Claude couldn't find an invoice after all retries — still show a card
                logger.warning(f"[extract] page {pg}: no invoice found after {EXTRACTION_RETRY_LIMIT} attempts — creating placeholder")
                placeholder = _make_placeholder(pg, layer, conf, reason="extraction_failed")
                placeholder["_retry_count"] = EXTRACTION_RETRY_LIMIT
                all_invoices.append(placeholder)

        state["extracted_invoices"] = all_invoices
        logger.info(f"[extract] scanned_pdf total: {len(all_invoices)} invoice(s)")
        return state

    # ── Digital PDFs, images, excel: extract each chunk independently ────────
    for i, chunk in enumerate(chunks):
        ocr_text   = chunk.get("text", "")
        confidence = chunk.get("confidence", 0.0)
        page_range = chunk.get("page_range", str(i + 1))
        layer_used = chunk.get("layer_used", "unknown")

        if not ocr_text.strip():
            logger.warning(f"[extract] Chunk {i} (page {page_range}) has no text — creating placeholder")
            all_invoices.append(_make_placeholder(page_range, layer_used, confidence, reason="ocr_empty"))
            continue

        logger.info(f"[extract] Chunk {i+1}/{len(chunks)} ({len(ocr_text)} chars, layer={layer_used})")

        # Retry loop
        best_invoices = []
        best_combined = 0.0
        attempt = 1

        _extract_from_text._last_math_issues = []   # reset per chunk
        for attempt in range(1, EXTRACTION_RETRY_LIMIT + 1):
            invoices = _extract_from_text(client, ocr_text, confidence, page_range, layer_used, attempt)
            if invoices:
                all_math_issues = []
                for inv in invoices:
                    all_math_issues.extend(inv.get("_math_issues") or [])
                _extract_from_text._last_math_issues = all_math_issues

                attempt_combined = max(
                    (float(inv.get("_ocr_confidence", 0.5)) + float(inv.get("confidence_score", 0.5))) / 2
                    for inv in invoices
                )
                if attempt_combined > best_combined:
                    best_combined = attempt_combined
                    best_invoices = invoices
                if best_combined >= CONFIDENCE_TARGET and not all_math_issues:
                    logger.info(f"[extract] Chunk {i} hit confidence target on attempt {attempt}: {best_combined:.2f}")
                    break
                if all_math_issues:
                    logger.info(f"[extract] Chunk {i} attempt {attempt}: math failed — retrying with error context")
            else:
                logger.warning(f"[extract] Chunk {i} attempt {attempt} returned empty — retrying")

            if attempt < EXTRACTION_RETRY_LIMIT:
                logger.info(f"[extract] Chunk {i} attempt {attempt} conf={best_combined:.2f} retrying...")

        if best_invoices:
            for inv in best_invoices:
                inv["_retry_count"] = attempt - 1
            all_invoices.extend(best_invoices)
        else:
            logger.warning(f"[extract] Chunk {i} (page {page_range}): no invoice after {EXTRACTION_RETRY_LIMIT} attempts — placeholder")
            placeholder = _make_placeholder(page_range, layer_used, confidence, reason="extraction_failed")
            placeholder["_retry_count"] = EXTRACTION_RETRY_LIMIT
            all_invoices.append(placeholder)

    state["extracted_invoices"] = all_invoices
    logger.info(f"[extract] Total invoices: {len(all_invoices)}")
    return state


def _make_placeholder(page_range, layer_used: str, ocr_confidence: float, reason: str) -> Dict:
    """
    Create a blank invoice placeholder for a page that could not be extracted.
    Shows up in the UI as a Manual Review card so the CA knows a page was missed.
    reason: 'ocr_empty' | 'extraction_failed'
    """
    reason_msg = {
        "ocr_empty":         f"Page {page_range}: OCR returned no text — image may be blank, rotated, or unreadable.",
        "extraction_failed": f"Page {page_range}: AI could not find invoice data after {EXTRACTION_RETRY_LIMIT} attempts — please fill manually.",
    }.get(reason, f"Page {page_range}: extraction failed.")

    return {
        "vendor_name":       None,
        "vendor_gstin":      None,
        "vendor_address":    None,
        "buyer_name":        None,
        "buyer_gstin":       None,
        "buyer_address":     None,
        "invoice_number":    None,
        "invoice_date":      None,
        "place_of_supply":   None,
        "line_items":        [],
        "taxable_value":     None,
        "cgst":              None,
        "sgst":              None,
        "igst":              None,
        "total_gst":         None,
        "invoice_total":     None,
        "grand_total":       None,
        "transaction_type":  None,
        "bank_details":      {"account_number": None, "ifsc_code": None, "bank_name": None, "branch": None, "account_type": None},
        "back_calculated":   False,
        "back_calc_note":    None,
        "confidence_score":  0.0,
        "field_confidence":  {},
        "issues":            [reason_msg],
        "_ocr_confidence":   ocr_confidence,
        "_source_pages":     str(page_range),
        "_layer_used":       layer_used,
        "_placeholder":      True,   # flag so route_node can set manual_review_required
    }


def _fix_gstin_ocr(raw: Optional[str]) -> Optional[str]:
    """
    Fix common OCR character substitutions in Indian GSTINs.
    GSTIN = 15 chars: NN AAAAA NNNN A Z A
    positions 0-1 are digits (state code), rest mix of alpha/digit.
    """
    if not raw:
        return raw
    s = raw.strip().upper().replace(" ", "")
    if len(s) < 15:
        return raw  # too short to fix reliably
    s = s[:15]  # truncate to 15

    # Positions 0,1 = state code digits: O→0, I→1, l→1
    fixed = list(s)
    for pos in (0, 1):
        if fixed[pos] == "O":
            fixed[pos] = "0"
        elif fixed[pos] in ("I", "l"):
            fixed[pos] = "1"

    # Positions 9–12 = numeric sequence in PAN structure: O→0 common OCR swap
    for pos in range(9, 13):
        if fixed[pos] == "O":
            fixed[pos] = "0"

    return "".join(fixed)


def _validate_gstin_checksum(gstin: Optional[str]) -> bool:
    """
    Validate a 15-character GSTIN using the official check-digit algorithm.

    Algorithm (GST Council specification):
      - Characters are indexed from the set: 0-9 A-Z (36 chars total, index 0-35)
      - Alternating factor 1, 2, 1, 2, … applied to chars 0-13
      - product = char_index × factor
      - contribution = (product // 36) + (product % 36)
      - check_index = (36 - (sum % 36)) % 36
      - The 15th character (index 14) must equal CHARS[check_index]

    Returns True if checksum passes, False otherwise.
    """
    CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if not gstin or len(gstin) != 15:
        return False
    gstin = gstin.upper()
    if not all(c in CHARS for c in gstin):
        return False

    total = 0
    factor = 1
    for c in gstin[:14]:
        digit = CHARS.index(c)
        product = digit * factor
        total += (product // 36) + (product % 36)
        factor = 2 if factor == 1 else 1

    expected_index = (36 - (total % 36)) % 36
    return CHARS[expected_index] == gstin[14]


def _validate_invoice_math(inv: Dict) -> List[str]:
    """
    Cross-check invoice arithmetic.  Returns a list of human-readable issue
    strings for every inconsistency found.  Empty list = all checks passed.

    Checks:
      1. CGST + SGST + IGST  ≈  total_gst          (within ₹2)
      2. taxable_value + total_gst  ≈  invoice_total (within ₹2)
      3. sum(line_items.taxable_value)  ≈  invoice.taxable_value  (within ₹5)

    Why ₹2 / ₹5 tolerance?  Rounding differences are legal in Indian invoices
    (each line rounded separately), so exact equality is too strict.
    """
    issues: List[str] = []

    def f(key: str) -> Optional[float]:
        return inv.get(key)

    cgst         = f("cgst")
    sgst         = f("sgst")
    igst         = f("igst")
    total_gst    = f("total_gst")
    taxable_val  = f("taxable_value")
    invoice_tot  = f("invoice_total")
    line_items   = inv.get("line_items") or []

    # ── Check 1: component GSTs sum to total_gst ──────────────────────
    gst_parts = [x for x in [cgst, sgst, igst] if x is not None]
    if gst_parts and total_gst is not None:
        gst_sum = sum(gst_parts)
        if abs(gst_sum - total_gst) > 2.0:
            issues.append(
                f"GST component mismatch: CGST({cgst}) + SGST({sgst}) + IGST({igst})"
                f" = {gst_sum:.2f}, but total_gst = {total_gst:.2f}"
                f" — likely a transposed or misread digit"
            )

    # ── Check 2: taxable_value + total_gst = invoice_total ────────────
    if taxable_val is not None and total_gst is not None and invoice_tot is not None:
        expected = taxable_val + total_gst
        if abs(expected - invoice_tot) > 2.0:
            issues.append(
                f"Invoice total mismatch: taxable({taxable_val:.2f}) + gst({total_gst:.2f})"
                f" = {expected:.2f}, but invoice_total = {invoice_tot:.2f}"
                f" — likely a transposed or misread digit"
            )

    # ── Check 3: line item subtotals vs header taxable_value ──────────
    if line_items and taxable_val is not None:
        li_tv_sum = sum(
            float(li.get("taxable_value") or 0)
            for li in line_items
            if li.get("taxable_value") is not None
        )
        if li_tv_sum > 0 and abs(li_tv_sum - taxable_val) > 5.0:
            issues.append(
                f"Line items taxable sum ({li_tv_sum:.2f}) ≠ header taxable_value"
                f" ({taxable_val:.2f}) — check for misread amounts in line items"
            )

    return issues


def _parse_indian_amount(val) -> Optional[float]:
    """
    Parse Indian invoice amount formats including shopkeeper notation.

    Handles:
      Standard:        "1,23,456.78" → 123456.78
      Paise with =:    "36=90"  → 36.90   "483=80" → 483.80
      Paise with -:    "300-89" → 300.89
      "Only" suffixes: "410="   → 410.0   "600-"  → 600.0
                       "60/"    → 60.0    "300/-" → 300.0
                       "300-/"  → 300.0   "60."   → 60.0
      Currency prefix: "₹300"   → 300.0   "Rs.300" → 300.0
      Already numeric: 300      → 300.0
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)

    import re
    s = str(val).strip()

    # Strip currency prefixes
    s = re.sub(r'^[₹\s]*', '', s)
    s = re.sub(r'^Rs\.?\s*', '', s, flags=re.IGNORECASE)

    # Remove thousand separators (commas in Indian numbering)
    s = s.replace(',', '')

    s = s.strip()
    if not s:
        return None

    # ── "Only" suffix patterns (trailing -, =, /, /-, -/, ^, ^/) ──
    # Check BEFORE paise-separator check. "410=" ends with = and nothing after.
    # "300-/" and "300/-" end with the sequence.
    only_suffix = re.compile(r'^(\d+(?:\.\d+)?)\s*[-=^/]{1,2}$')
    m = only_suffix.match(s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass

    # ── Paise separator: NUMBER[=-]TWODIGS ─────────────────────────
    # "36=90" → 36.90,  "483=80" → 483.80,  "300-89" → 300.89
    # The separator must be EXACTLY = or - (not combined), followed by exactly 2 digits.
    paise_sep = re.compile(r'^(\d+)[=\-](\d{2})$')
    m = paise_sep.match(s)
    if m:
        try:
            return float(f"{m.group(1)}.{m.group(2)}")
        except ValueError:
            pass

    # ── Standard float / integer ────────────────────────────────────
    # Strip any remaining trailing non-numeric junk (extra OCR noise)
    s_clean = re.sub(r'[^0-9.]', '', s)
    try:
        return float(s_clean) if s_clean else None
    except ValueError:
        return None


def _safe_json_parse(raw: str) -> Optional[Dict]:
    """
    Robustly parse JSON from Claude's response.

    Handles two failure modes:
    1. "Extra data" — Claude emitted two JSON objects back-to-back.
       We extract just the first complete object using a brace-depth counter.
    2. Truncated JSON — Claude hit max_tokens mid-object.
       We log and return None so the caller can retry.
    """
    # Fast path: valid JSON
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        pass

    # Try to extract the first complete { ... } block
    try:
        depth = 0
        start = raw.index("{")
        for i, ch in enumerate(raw[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = raw[start: i + 1]
                    result = json.loads(candidate)
                    logger.warning(f"[extract] Recovered first JSON object ({len(candidate)} chars) from multi-block response")
                    return result
    except (ValueError, json.JSONDecodeError) as e:
        logger.error(f"[extract] JSON recovery failed: {e} — response may be truncated")

    return None


def _extract_from_text(
    client: anthropic.Anthropic,
    ocr_text: str,
    ocr_confidence: float,
    page_range: str,
    layer_used: str,
    attempt: int = 1,
) -> List[Dict]:
    """Call Claude Haiku and parse the JSON response."""
    try:
        # Add retry hint to prompt on retries
        # On attempt 2+, include the specific math discrepancies from the previous
        # attempt so Claude knows exactly which numbers to re-read digit by digit.
        if attempt == 1:
            extra = ""
        else:
            extra = (
                f"\n\n⚠️  RETRY ATTEMPT {attempt} — the previous extraction had arithmetic errors."
                "\nPlease re-read EVERY digit in the amounts below very carefully from the source text."
                "\nCommon OCR mistakes: 4↔9, 1↔7, 6↔0, 8↔3, digit transpositions (e.g. 4823→4832)."
            )
            # Include the specific math failures if caller passed them
            prev_issues = _extract_from_text._last_math_issues if hasattr(_extract_from_text, "_last_math_issues") else []
            if prev_issues:
                extra += "\n\nSpecific discrepancies found:\n" + "\n".join(f"  • {i}" for i in prev_issues)
            extra += (
                "\n\nVerify: taxable_value + total_gst = invoice_total."
                "\nVerify: CGST + SGST = total_gst (intrastate) or IGST = total_gst (interstate)."
                "\nIf unsure of a digit, prefer the value that makes the arithmetic consistent."
            )

        # For combined multi-page docs allow more context (up to 24k chars)
        max_text_chars = 24000
        truncated_text = ocr_text[:max_text_chars]
        if len(ocr_text) > max_text_chars:
            logger.warning(f"[extract] Text truncated from {len(ocr_text)} to {max_text_chars} chars")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=6000,  # some invoices have 20+ line items; 4096 was truncating JSON
            temperature=0,    # deterministic — same OCR text always → same extraction
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(ocr_text=truncated_text) + extra,
            }],
        )

        track_claude_cost(
            model="claude-haiku",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        parsed = _safe_json_parse(raw)
        if parsed is None:
            logger.error(f"[extract] Could not parse JSON on attempt {attempt}")
            return []
        invoices_data = parsed.get("invoices", [parsed] if "vendor_name" in parsed else [])

        result = []
        for inv in invoices_data:
            # Normalise taxable_value field name
            if "taxable_value_total" in inv and "taxable_value" not in inv:
                inv["taxable_value"] = inv.pop("taxable_value_total")

            # Post-process: fix common OCR mistakes in GSTINs
            for gstin_field in ("vendor_gstin", "buyer_gstin"):
                inv[gstin_field] = _fix_gstin_ocr(inv.get(gstin_field))

            # Post-process: normalise Indian number formatting in amounts
            for amt_field in ("taxable_value", "cgst", "sgst", "igst", "total_gst", "invoice_total", "grand_total"):
                inv[amt_field] = _parse_indian_amount(inv.get(amt_field))

            # ── GSTIN checksum validation ─────────────────────────────
            if not inv.get("field_confidence"):
                inv["field_confidence"] = {}
            for gstin_field in ("vendor_gstin", "buyer_gstin"):
                gv = inv.get(gstin_field)
                if gv and not _validate_gstin_checksum(gv):
                    inv.setdefault("issues", []).append(
                        f"{gstin_field} '{gv}' fails checksum — OCR likely misread a digit"
                    )
                    # Drop field confidence so reviewer is alerted
                    inv["field_confidence"][gstin_field] = min(
                        float(inv["field_confidence"].get(gstin_field) or 0.5), 0.35
                    )
                    logger.warning(f"[extract] {gstin_field} checksum FAIL: {gv}")

            # ── Invoice arithmetic cross-check ────────────────────────
            math_issues = _validate_invoice_math(inv)
            if math_issues:
                inv.setdefault("issues", []).extend(math_issues)
                # Lower overall confidence — if the numbers don't add up, something
                # was misread and the CA must verify.
                old_conf = float(inv.get("confidence_score") or 0.5)
                inv["confidence_score"] = min(old_conf, 0.55)
                for issue in math_issues:
                    logger.warning(f"[extract] Math check FAIL (page {page_range}): {issue}")

            # Tag source metadata
            inv["_ocr_confidence"]  = ocr_confidence
            inv["_source_pages"]    = page_range
            inv["_layer_used"]      = layer_used
            inv["_math_issues"]     = math_issues   # passed to retry prompt

            # Ensure bank_details always present
            if "bank_details" not in inv or not inv["bank_details"]:
                inv["bank_details"] = {
                    "account_number": None, "ifsc_code": None,
                    "bank_name": None, "branch": None, "account_type": None,
                }

            result.append(inv)

        return result

    except anthropic.APIError as e:
        error_str = str(e)
        # Fatal billing/auth errors — no point retrying, raise immediately so the
        # caller can abort the whole job rather than hammering the API 3× per page
        if any(kw in error_str for kw in ("credit balance", "billing", "quota", "insufficient_quota", "UNAUTHENTICATED")):
            logger.error(f"[extract] FATAL billing error — aborting: {e}")
            raise  # propagate up to abort the job
        logger.error(f"[extract] Anthropic API error: {e}")
        return []
    except Exception as e:
        logger.error(f"[extract] Unexpected error: {e}")
        return []
