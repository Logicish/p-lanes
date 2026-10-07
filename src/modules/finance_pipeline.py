# modules/finance_pipeline.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/22/2026
#
# ==================================================
# Finance file ingestion pipeline + chat enricher.
#
# Watches /var/lib/p-lanes/users/root/dump/finance/
# for PDF, TXT, and CSV bank/credit/loan statements.
#
# Pipeline steps:
#   1. Extract text from file (pymupdf / csv parser / passthrough)
#   2. LLM extracts account header (name, type, last four,
#      APR, ending balance, statement month)
#   3. LLM extracts transactions from chunked text
#      (CSV uses programmatic parse when columns detected)
#   4. Bucket classifier assigns bucket1 to each transaction
#      using hardcoded pattern rules (no LLM)
#   5. Writes to finance.db: accounts + transactions
#   6. Moves processed file to done/ (failures → done/failed/)
#
# Bucket rules:
#   mortgage     — freddie mac, rocket mortgage
#   utilities    — cleco, at&t, tammany, trash
#   entertainment — spotify, amazon prime
#   maintenance  — home depot, lowes, autozone, dealerships
#   food         — uber eats, dominos, vending
#   retail       — amazon, aliexpress, walmart, costco, paypal
#   work_income  — lcmc health (credit only)
#   misc_income  — all other credits
#   transfer     — zelle, wire transfers
#   other        — fallback
#
# Venmo special case (amount-based):
#   ≤ $10       → food   (snack cart)
#   $10–$100    → maintenance (grass cutting)
#   ≥ $200      → transfer (spouse)
#   else        → other
#
# Triggered by chat intent: finance_process
# Security: ADMIN (4) — root only
#
# Knows about: core/events, core/pipeline, core/llm,
#              providers (get_db("finance")).
# ==================================================

# ==================================================
# Imports
# ==================================================
import csv
import io
import json
import os
import re
import shutil
from pathlib import Path

import fitz  # pymupdf
import structlog

import providers
from config import SecurityLevel
from core import llm
from core.events import register
from core.pipeline import PipelineContext

log = structlog.get_logger()

# ==================================================
# Paths
# ==================================================
DUMP_DIR        = Path("/var/lib/p-lanes/users/root/dump/finance")
DONE_DIR        = DUMP_DIR / "done"
DONE_FAILED_DIR = DUMP_DIR / "done" / "failed"
SUPPORTED_EXT   = {".pdf", ".txt", ".csv"}

# max chars fed to the LLM per transaction chunk
_CHUNK_CHARS = 1800

# max chars of account header text sent for account extraction
_ACCOUNT_HEADER_CHARS = 800

# ==================================================
# Bucket classification
# ==================================================
_VENMO_FOOD_MAX         = 10.0
_VENMO_MAINTENANCE_MAX  = 100.0
_VENMO_TRANSFER_MIN     = 200.0

# Ordered list of (patterns, bucket1).
# Checked case-insensitively against "{issuer} {description}".
# More specific patterns must appear before broader overlaps.
_BUCKET_PATTERNS: list[tuple[list[str], str]] = [
    # interest — card interest charges (negative) and savings/account interest earned (positive)
    (["purchase interest charge", "interest charge", "interest fee",
      "monthly interest paid", "interest earned", "interest payment"],
     "interest"),
    # mortgage
    (["freddie mac", "freddy mac", "rocket mortgage", "rocketmortgage",
      "kfcu mortgage", "keesler fcu mortgage",
      "withdrawal trans to loan", "trans to loan"],
     "mortgage"),
    # pets — must be before utilities ("tammany"/"northshore" would match humane societies)
    (["petsmart", "petco", "banfield", "chewy", "chwy",
      "humane society", "humane soc", "spca",
      "st tammany humane", "tammany humane", "northshore humane",
      "petland", "pet supplies", "pet food",
      "veterina", "animal hosp", "animal clinic", "animal medical",
      "tractor supply",
      "honest paws"],
     "pets"),
    # utilities (includes insurance)
    # "tammany parish" instead of bare "tammany" to avoid matching humane societies
    (["cleco", "at&t", "att*", "att ", "tammany parish", "northshore", "trash", "waste management",
      "geico", "progressive", "state farm", "allstate", "nationwide",
      "insurance", "nso hpso", "hpso ins"],
     "utilities"),
    # entertainment (amazon prime before generic amazon)
    (["spotify", "amazon prime", "amazon prime video",
      "google play", "google *play", "google*play",
      "apple music", "netflix", "hulu", "disney", "hbo", "youtube premium",
      "gog.com", "gog sp.", "gog sp. z", "paddle.net", "steam ", "steampowered",
      "twitch", "playstation", "xbox game", "nintendo",
      "gun range", "shooting range", "pac gun", "range llc",
      "vape", "vapor", "esmoke", "vapejuice", "ejuice",
      "claude.ai", "movietavern", "movie tavern", "painting with a twist",
      "gamesplanet", "cloudflare", "google *post", "google*post",
      "global wildlife", "audubon"],
     "entertainment"),
    # car loan — must be before generic "capital one" cards check
    (["capital one auto", "capital one carpay", "capital one car"],
     "car_loan"),
    # home improvement
    (["enerbank", "home improvement", "ocooch", "hardwood", "lumber",
      "nursery", "landscape", "landscaping", "bantings", "lily oak"],
     "home_improvement"),
    # travel_cost — gas, fuel, tolls, parking
    (["exxon", "chevron", "shell ", "bp ", "sunoco", "speedway", "valero",
      "marathon petro", "quiktrip", "wawa", "pilot travel", "bargain stop",
      "racetrac", "murphy express", "murphy usa", "circle k", "sheetz",
      "causeway toll", "causeway", "toll ", "e-zpass", "sunpass", "ipass", "fastrak",
      "parking ", "park meter", "parkway",
      "marathon 2", "texaco", "buc-ee", "bucee",
      "delta air", "southwest air", "united air", "american air", "spirit air",
      "allianz travel", "travel ins",
      "homewood suites", "hilton ", "marriott", "hampton inn", "holiday inn",
      "msy/noab", "noab", "la inter service fee", "airport"],
     "travel_cost"),
    # maintenance — auto service + home services (gas moved to travel_cost)
    (["home depot", "lowe's", "lowes", "lowe ", "autozone", "o'reilly",
      "jiffy lube", "oil change", "take 5", "firestone", "goodyear", "midas",
      "pep boys", "dealership", "toyota", "honda", "ford", "chevrolet",
      "nissan", "dodge",
      "car wash", "car wa",
      "exterminating", "pest control", "orkin", "terminix",
      "faithful exterior", "autobks",
      "vehicle registratio", "tag renewal",
      "homedepot.com"],
     "maintenance"),
    # health / fitness / beauty / labs
    (["snap fitness", "planet fitness", "anytime fitness", "gym",
      "urgent care", "clinic", "hospital", "pharmacy",
      "cvs", "walgreens", "rite aid", "optum", "united health",
      "blue cross", "cigna", "aetna", "medicare",
      "betterhelp", "bh* better", "labcorp", "quest diagnostics",
      "america's best", "americas best", "beaute", "direct peptide",
      "mms-lcmc", "mms lcmc"],
     "health"),
    # food (delivery, restaurants, groceries)
    (["uber eats", "ubereats", "uber*eats", "doordash", "grubhub",
      "domino's", "dominos", "pizza hut", "vending", "snack cart",
      "rouses", "winn dixie", "winn-dixie", "whole foods", "trader joe",
      "publix", "kroger", "sprouts", "aldi",
      "coca cola", "starbucks", "chick-fil-a", "chickfila", "mcdonald",
      "taco bell", "subway", "wendy's", "wendys", "burger king",
      "popeyes", "raising cane", "raising canes",
      "dragos", "chimes", "georges mexican", "busters",
      "habaneros", "crumbl", "five guys", "chipotle", "panera",
      "ihop", "applebee's", "applebees", "olive garden", "red lobster",
      "outback", "longhorn", "texas road", "chili's", "chilies"],
     "food"),
    # retail (walmart = retail, not food; amazon after amazon prime check)
    (["amazon", "amzn", "aliexpress", "walmart", "wal-mart",
      "sam's club", "samsclub", "costco", "paypal",
      "ebay", "nordstrom", "macy's", "macys", "tj maxx", "tjmaxx",
      "marshalls", "burlington", "old navy", "gap ", "target ",
      "sweetwater", "great big canvas", "1-800-flowers", "ruths roses",
      "academy sports", "academy #",
      "wm supercenter", "walmart supercenter",
      "adafruit", "digi key", "digikey", "newark corp", "mouser", "sparkfun",
      "woobles", "aquatic arts", "gutter supply", "hobby lobby", "michaels"],
     "retail"),
    # student loans — from loan servicer account statements (AllLoans.csv etc.)
    # advs ed serv is intentionally NOT here; that pattern appears in checking
    # as a payment TO the servicer and belongs in transfer (see below)
    (["direct loan", "subsidized", "unsubsidized", "aidvantage",
      "student loan", "mohela", "nelnet", "great lakes"],
     "student_loans"),
    # credit card payments (own-account transfer — both directions).
    # "payment thank you" covers Chase's in-statement payment credit lines.
    (["capital one", "chase credit", "chase credit crd",
      "payment thank you",
      "citi card", "citicard", "discover card", "barclays",
      "american express", "amex"],
     "cards"),
    # savings transfers — both directions; net to zero across uploads.
    # Include destination account names so savings-side "withdrawal to checking"
    # is also recognized as a savings transfer.
    (["anthony root web", "keesler fcu", "xxxxx2831", "360 performance savings",
      "360 savings", "ally savings", "ally bank",
      "marcus savings", "credit union savings",
      "total control checking"],          # savings→checking leg
     "savings"),
    # retirement / investment transfers — both directions
    (["fidelity", "fid bkg svc", "vanguard", "schwab", "tiaa",
      "401k", "ira transfer", "retirement"],
     "retirement"),
    # income — work (credit only, enforced in classify fn)
    (["lcmc health", "lcmc"],
     "work_income"),
    # transfer — inter-account moves that don't fit a named bucket above,
    # plus loan servicer ACH entries from checking (advs ed serv, etc.)
    # These are own-account transfers; the loan statement is the source of truth.
    (["zelle", "wire transfer",
      "advs ed serv", "advs ed",
      "withdrawal to 360", "transfer to 360"],
     "transfer"),
]

# Buckets that represent money moving between the user's own accounts.
# These are excluded from expense/income totals — both directions net to zero.
TRANSFER_BUCKETS: frozenset[str] = frozenset({
    "transfer",
    "savings",
    "retirement",
    "cards",
})


def _classify_bucket(issuer: str, description: str, amount: float) -> str:
    combined = f"{issuer} {description}".lower()

    # Venmo: amount-based routing
    if "venmo" in combined:
        abs_amt = abs(amount)
        if abs_amt <= _VENMO_FOOD_MAX:
            return "food"
        if abs_amt <= _VENMO_MAINTENANCE_MAX:
            return "maintenance"
        if abs_amt >= _VENMO_TRANSFER_MIN:
            return "transfer"
        return "other"

    # Work income only on credits
    if amount > 0 and any(p in combined for p in ["lcmc health", "lcmc"]):
        return "work_income"

    for patterns, bucket in _BUCKET_PATTERNS:
        if bucket == "work_income":
            continue  # handled above
        if any(p in combined for p in patterns):
            return bucket

    # Unmatched credit → misc income
    if amount > 0:
        return "misc_income"

    return "other"


# ==================================================
# LLM prompts
# ==================================================
_ACCOUNT_PROMPT = """\
Extract the account summary from this bank/credit/loan statement.
Return ONLY valid JSON — no markdown, no explanation:
{
  "account_name": "institution name e.g. Chase Bank",
  "friendly_name": "short name e.g. Chase Checking ...1234",
  "account_type": "checking|savings|credit|loan|other",
  "last_four": "1234 or null",
  "apr": 24.99,
  "ending_balance": 1234.56,
  "statement_month": "YYYY-MM"
}

Rules:
- account_name: bank or servicer name only, not the account holder name
- friendly_name: 2-4 words + last four digits if available
- apr: null for checking/savings; annual interest rate number for credit/loans
- ending_balance: closing or statement balance (not available credit)
- statement_month: YYYY-MM from statement period end date

Statement header:
"""

_TRANSACTIONS_PROMPT_TEMPLATE = """\
Extract all individual transactions from this bank/credit statement excerpt.
Return ONLY a valid JSON array — no markdown, no explanation:
[{{"date":"YYYY-MM-DD","amount":-12.34,"issuer":"RAW MERCHANT","friendly_name":"Clean Name","description":"full raw line"}}]

Rules:
- amount: NEGATIVE for charges/debits/withdrawals, POSITIVE for credits/deposits/payments
- date: YYYY-MM-DD format (statement year: {year})
- issuer: raw merchant or payee name as it appears in the statement
- friendly_name: short clean readable name (e.g. "Amazon", "Domino's", "LCMC Health")
- description: full raw transaction line from the statement
- SKIP: balance lines, fee summaries, opening/closing balance rows, page headers
- Return [] if no transactions in this excerpt

Excerpt:
"""


def _strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


# ==================================================
# Text extraction
# ==================================================

def _extract_pdf(path: str) -> list[str]:
    """Return list of page text strings from a PDF."""
    doc = fitz.open(path)
    pages = []
    for page in doc:
        text = page.get_text()
        if text.strip():
            pages.append(text)
    doc.close()
    return pages


def _chunk_text(text: str, chunk_chars: int = _CHUNK_CHARS) -> list[str]:
    """Split plain text into chunks at line boundaries."""
    lines = text.splitlines(keepends=True)
    chunks, current, size = [], [], 0
    for line in lines:
        if size + len(line) > chunk_chars and current:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


# CSV column detection
# Ordered tuples: first match wins (priority matters when a file has multiple date columns)
_DATE_HEADERS   = (
    "transaction date", "trans date", "trans. date",
    "date", "posted date", "posting date",
)
_AMT_HEADERS    = {"amount", "transaction amount", "amt", "total", "net amount", "net"}
_DEBIT_HEADERS  = {"debit", "withdrawal", "withdrawals", "charges"}
_CRED_HEADERS   = {"credit", "deposit", "deposits", "payments", "payment"}
_DESC_HEADERS   = {"description", "memo", "transaction description", "payee", "name", "narrative", "details"}
# "category" intentionally excluded — bank-assigned categories (e.g. "Utilities") are not issuer names
_ISSUER_HEADERS = {"merchant", "merchant name", "payee name", "loanname", "loan name", "vendor"}
_TYPE_HEADERS   = {"transaction type", "type", "dr/cr", "debit or credit", "transaction code"}
# Columns that carry the card/account last-four digits
_CARD_NO_HEADERS = {"card no.", "card no", "card number", "account number", "account no.", "account no"}

# Debit indicator values in a transaction type column
_DEBIT_VALUES = {"debit", "dr", "withdrawal", "debit card", "check", "ach debit"}

# Leading bank action phrases to strip from descriptions for cleaner issuer names
_DESC_PREFIX_RE = re.compile(
    r"^(?:withdrawal from|deposit from|transfer from|transfer to|"
    r"payment to|payment from|direct deposit from|direct debit to|"
    r"ach debit|ach credit|pos purchase[\s\-]*|digital card purchase[\s\-]*|"
    r"check deposit|mobile deposit|online transfer(?:\s+to)?)\s*",
    re.IGNORECASE,
)


def _parse_csv(path: str) -> tuple[str, list[dict]]:
    """Parse a CSV statement. Returns (account_hint, rows).

    account_hint: pre-header rows joined for LLM account extraction.
    rows: list of dicts with keys date, amount, issuer, description.
    """
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        raw = f.read()

    # Strip HTML DOCTYPE preamble if a browser exported the CSV with it
    raw = re.sub(r'^<!DOCTYPE[^>]*>', '', raw.lstrip())

    lines = raw.splitlines()

    # Find the header row (first row where 2+ cells match known column names)
    header_idx = None
    for i, line in enumerate(lines):
        try:
            cells = next(csv.reader([line]))
        except Exception:
            continue
        lower_cells = {c.strip().lower() for c in cells}
        matches = (
            lower_cells.intersection(_DATE_HEADERS) and
            (lower_cells & (_AMT_HEADERS | _DEBIT_HEADERS | _CRED_HEADERS)) and
            (lower_cells & _DESC_HEADERS)
        )
        if matches:
            header_idx = i
            break

    account_hint = "\n".join(lines[:header_idx]) if header_idx else ""

    if header_idx is None:
        # No recognized header — return raw text for LLM processing
        return account_hint, []

    data_text = "\n".join(lines[header_idx:])
    reader = csv.DictReader(io.StringIO(data_text))
    fields_lower = {k.strip().lower(): k for k in (reader.fieldnames or [])}

    date_col    = next((fields_lower[h] for h in _DATE_HEADERS    if h in fields_lower), None)
    desc_col    = next((fields_lower[h] for h in _DESC_HEADERS    if h in fields_lower), None)
    amt_col     = next((fields_lower[h] for h in _AMT_HEADERS     if h in fields_lower), None)
    dbt_col     = next((fields_lower[h] for h in _DEBIT_HEADERS   if h in fields_lower), None)
    crd_col     = next((fields_lower[h] for h in _CRED_HEADERS    if h in fields_lower), None)
    issuer_col  = next((fields_lower[h] for h in _ISSUER_HEADERS  if h in fields_lower), None)
    type_col    = next((fields_lower[h] for h in _TYPE_HEADERS    if h in fields_lower), None)
    card_no_col = next((fields_lower[h] for h in _CARD_NO_HEADERS if h in fields_lower), None)

    rows = []
    card_no_hint = ""  # populated from first data row if card_no_col found
    for row in reader:
        if not row:
            continue
        date_val   = row.get(date_col,   "").strip() if date_col   else ""
        desc_val   = row.get(desc_col,   "").strip() if desc_col   else ""
        issuer_val = row.get(issuer_col, "").strip() if issuer_col else ""
        type_val   = row.get(type_col,   "").strip().lower() if type_col else ""
        if not date_val:
            continue

        # Capture card/account number from first data row for account extraction
        if card_no_col and not card_no_hint:
            card_no_hint = row.get(card_no_col, "").strip()

        def _clean_amt(s: str) -> float:
            s = s.strip().replace(",", "").replace("$", "").replace(" ", "")
            return float(s) if s and s not in ("-", "+") else 0.0

        amount = None
        if amt_col and row.get(amt_col, "").strip():
            try:
                amount = _clean_amt(row[amt_col])
                # Apply sign from type column when amount has no intrinsic sign
                if amount > 0 and type_val and type_val in _DEBIT_VALUES:
                    amount = -amount
            except ValueError:
                pass
        elif dbt_col or crd_col:
            try:
                debit  = _clean_amt(row.get(dbt_col) or "0")
                credit = _clean_amt(row.get(crd_col) or "0")
                if debit:
                    amount = -abs(debit)
                elif credit:
                    amount = abs(credit)
            except ValueError:
                pass

        if amount is None or amount == 0.0:
            continue

        # Clean up description — strip leading bank action phrases
        raw_desc    = desc_val or issuer_val
        clean_desc  = _DESC_PREFIX_RE.sub("", raw_desc).strip() or raw_desc
        issuer_name = issuer_val if issuer_val else clean_desc

        rows.append({
            "date":          _normalize_date(date_val),
            "amount":        amount,
            "issuer":        issuer_name,
            "friendly_name": clean_desc,
            "description":   raw_desc,
        })

    # Append card number to hint so LLM can populate last_four
    if card_no_hint:
        account_hint = (account_hint + "\n" if account_hint else "") + f"Card last four digits: {card_no_hint}"

    return account_hint, rows


_DATE_RE = re.compile(
    r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})"  # MM/DD/YYYY or MM-DD-YYYY
    r"|(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})"    # YYYY-MM-DD
)


def _normalize_date(raw: str) -> str:
    """Attempt to normalize a date string to YYYY-MM-DD."""
    raw = raw.strip()
    m = _DATE_RE.search(raw)
    if not m:
        return raw
    if m.group(4):  # YYYY-MM-DD
        return f"{m.group(4)}-{int(m.group(5)):02d}-{int(m.group(6)):02d}"
    # MM/DD/YYYY or MM/DD/YY
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    return f"{year}-{month:02d}-{day:02d}"


# ==================================================
# LLM extraction
# ==================================================

async def _extract_account(text_head: str) -> dict | None:
    response = await llm.call_internal(
        messages=[{"role": "user", "content": _ACCOUNT_PROMPT + text_head[:_ACCOUNT_HEADER_CHARS]}],
        temperature=0.1,
        max_tokens=256,
    )
    try:
        return json.loads(_strip_json_fences(response.content))
    except Exception as e:
        log.error("finance_account_parse_failed", error=str(e), raw=response.content[:200])
        return None


async def _extract_transactions_chunk(chunk: str, year: str) -> list[dict]:
    prompt = _TRANSACTIONS_PROMPT_TEMPLATE.format(year=year) + chunk
    response = await llm.call_internal(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=2048,
    )
    try:
        result = json.loads(_strip_json_fences(response.content))
        return result if isinstance(result, list) else []
    except Exception as e:
        log.warning("finance_tx_chunk_parse_failed", error=str(e), raw=response.content[:200])
        return []


async def _extract_transactions_from_text(chunks: list[str], year: str) -> list[dict]:
    all_txs = []
    for i, chunk in enumerate(chunks):
        log.debug("finance_tx_chunk", chunk_num=i + 1, total=len(chunks))
        txs = await _extract_transactions_chunk(chunk, year)
        all_txs.extend(txs)
    return all_txs


# ==================================================
# DB operations
# ==================================================

async def _save_account(db, account: dict, source_file: str) -> int | None:
    """Upsert account.

    Match priority:
      1. last_four + account_type + statement_month  (LLM-stable; no new row on name drift)
      2. account_name + statement_month              (fallback for accounts without last_four)
    """
    last_four  = str(account.get("last_four") or "") or None
    acct_type  = account.get("account_type")
    stmt_month = account.get("statement_month", "")
    acct_name  = account.get("account_name", "Unknown")
    friendly   = account.get("friendly_name") or acct_name

    # --- Priority 1: match by last_four so LLM name drift never creates a duplicate ---
    if last_four:
        existing = await db.fetchone(
            "SELECT id FROM accounts WHERE last_four = ? AND account_type = ? AND statement_month = ?",
            (last_four, acct_type, stmt_month),
        )
        if existing:
            await db.execute(
                """UPDATE accounts SET
                       friendly_name  = ?,
                       apr            = ?,
                       ending_balance = ?,
                       source_file    = ?
                   WHERE id = ?""",
                (friendly, account.get("apr"), account.get("ending_balance"),
                 source_file, existing["id"]),
            )
            return existing["id"]

    # --- Priority 2: insert or update by account_name + statement_month ---
    await db.execute(
        """
        INSERT INTO accounts
            (friendly_name, account_name, account_type, last_four,
             apr, ending_balance, statement_month, source_file)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_name, statement_month) DO UPDATE SET
            friendly_name  = excluded.friendly_name,
            apr            = excluded.apr,
            ending_balance = excluded.ending_balance,
            source_file    = excluded.source_file
        """,
        (friendly, acct_name, acct_type, last_four,
         account.get("apr"), account.get("ending_balance"), stmt_month, source_file),
    )
    row = await db.fetchone(
        "SELECT id FROM accounts WHERE account_name = ? AND statement_month = ?",
        (acct_name, stmt_month),
    )
    return row["id"] if row else None


async def _save_transactions(db, account_id: int, txs: list[dict]) -> int:
    new_count = 0
    for tx in txs:
        issuer = (tx.get("issuer") or "").strip()
        desc   = (tx.get("description") or "").strip()
        amount = tx.get("amount", 0.0)
        bucket = _classify_bucket(issuer, desc, amount)
        try:
            row_id = await db.execute(
                """
                INSERT OR IGNORE INTO transactions
                    (account_id, friendly_name, date, amount,
                     issuer, description, bucket1, bucket2)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    account_id,
                    (tx.get("friendly_name") or issuer or "").strip() or None,
                    tx.get("date", ""),
                    amount,
                    issuer or None,
                    desc or None,
                    bucket,
                ),
            )
            if row_id:
                new_count += 1
        except Exception as e:
            log.warning("finance_tx_save_failed", error=str(e))
    return new_count


# ==================================================
# Main entry point
# ==================================================

async def process_file(path: str) -> dict | None:
    """Process one finance file. Returns summary dict or None on failure."""
    db = providers.get_db("finance")
    if db is None or not db.is_ready:
        log.error("finance_pipeline_no_db")
        return None

    file_path = Path(path)
    ext = file_path.suffix.lower()
    log.info("finance_pipeline_start", file=file_path.name, ext=ext)

    try:
        csv_rows: list[dict] = []
        account_hint = ""

        if ext == ".pdf":
            pages = _extract_pdf(path)
            if not pages:
                raise ValueError("PDF produced no text — may be scanned/image-only")
            account_text = pages[0][:_ACCOUNT_HEADER_CHARS]
            # For transactions: chunk each page independently so fine-print
            # pages don't bloat a single chunk beyond _CHUNK_CHARS.
            chunks = []
            for page in pages:
                chunks.extend(_chunk_text(page, _CHUNK_CHARS))

        elif ext == ".csv":
            account_hint, csv_rows = _parse_csv(path)
            if csv_rows:
                # Programmatic parse succeeded — give LLM the hint + first few rows
                # so it has enough context to name the account
                sample_rows = "\n".join(
                    f"{r['date']} | {r['description']} | {r['amount']}"
                    for r in csv_rows[:8]
                )
                account_text = (
                    (account_hint + "\n" if account_hint else "")
                    + f"File: {file_path.stem}\n"
                    + sample_rows
                )
                chunks = []
            else:
                # Column detection failed — fall back to LLM on raw text
                log.info("finance_csv_llm_fallback", file=file_path.name)
                raw_text = file_path.read_text(encoding="utf-8-sig", errors="replace")
                account_text = raw_text[:3000]
                chunks = _chunk_text(raw_text)

        elif ext == ".txt":
            text = file_path.read_text(encoding="utf-8", errors="replace")
            account_text = text[:3000]
            chunks = _chunk_text(text)

        else:
            raise ValueError(f"Unsupported extension: {ext}")

        # --- Account header ---
        account = await _extract_account(account_text)
        if not account or not account.get("statement_month"):
            # Salvage from csv_rows dates if available
            dates = [r["date"] for r in csv_rows if r.get("date")]
            if dates:
                months = {d[:7] for d in dates if len(d) >= 7}
                sm = list(months)[0] if len(months) == 1 else "ongoing"
                if account:
                    account["statement_month"] = sm
                else:
                    account = {
                        "account_name":   file_path.stem.replace("_", " "),
                        "friendly_name":  file_path.stem.replace("_", " "),
                        "account_type":   "other",
                        "last_four":      None,
                        "apr":            None,
                        "ending_balance": None,
                        "statement_month": sm,
                    }
            if not account or not account.get("statement_month"):
                raise ValueError("Could not extract account header or statement month")

        # For CSV files spanning multiple months: pin to "ongoing" so that
        # re-uploads always resolve to the same account record and duplicate
        # transactions are caught by the UNIQUE(account_id, date, amount, issuer)
        # constraint rather than creating a new account each month.
        if ext == ".csv" and csv_rows:
            dates = [r["date"] for r in csv_rows if r.get("date")]
            months = {d[:7] for d in dates if len(d) >= 7}
            if len(months) > 1:
                account["statement_month"] = "ongoing"

        year = (account.get("statement_month") or "")[:4] or "2026"

        account_id = await _save_account(db, account, file_path.name)
        if account_id is None:
            raise ValueError("Failed to save account record")

        # --- Transactions ---
        if csv_rows:
            txs = csv_rows
        else:
            txs = await _extract_transactions_from_text(chunks, year)

        # Normalize dates for CSV rows that skipped LLM
        for tx in txs:
            if tx.get("date") and len(tx["date"]) < 10:
                tx["date"] = _normalize_date(tx["date"])

        tx_count = await _save_transactions(db, account_id, txs)

        # Move to done/
        dest = DONE_DIR / file_path.name
        shutil.move(path, dest)

        log.info("finance_pipeline_done",
                 file=file_path.name,
                 account=account.get("friendly_name"),
                 transactions=tx_count)

        return {
            "file":         file_path.name,
            "friendly_name": account.get("friendly_name", account.get("account_name")),
            "statement_month": account.get("statement_month"),
            "transactions": tx_count,
        }

    except Exception as e:
        log.error("finance_pipeline_failed", file=file_path.name, error=str(e))
        try:
            shutil.move(path, DONE_FAILED_DIR / file_path.name)
        except Exception:
            pass
        return None


# ==================================================
# Rebucket — re-classify all existing transactions
# ==================================================

async def rebucket_all() -> int:
    """Re-run bucket classification on every transaction in finance.db.

    Useful after adding or editing bucket patterns. Returns count updated.
    """
    db = providers.get_db("finance")
    if db is None or not db.is_ready:
        log.error("finance_rebucket_no_db")
        return 0

    rows = await db.fetchall("SELECT id, issuer, description, amount FROM transactions", ())
    updated = 0
    for row in rows:
        new_bucket = _classify_bucket(
            row["issuer"] or "",
            row["description"] or "",
            row["amount"],
        )
        if new_bucket != row.get("bucket1"):
            await db.execute(
                "UPDATE transactions SET bucket1 = ? WHERE id = ?",
                (new_bucket, row["id"]),
            )
            updated += 1

    log.info("finance_rebucket_done", updated=updated, total=len(rows))
    return updated


# ==================================================
# Enricher — finance_process intent
# ==================================================

@register("finance_process", "enricher")
async def handle(ctx: PipelineContext) -> PipelineContext:
    if ctx.intent != "finance_process":
        return ctx

    if ctx.user.security_level < SecurityLevel.ADMIN:
        ctx.enrichments.append({
            "source":  "finance",
            "content": "Finance processing requires admin access.",
        })
        return ctx

    if not DUMP_DIR.exists():
        ctx.enrichments.append({
            "source":  "finance",
            "content": f"Finance dump folder does not exist: {DUMP_DIR}",
        })
        return ctx

    pending = [
        f for f in DUMP_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXT
    ]

    if not pending:
        ctx.enrichments.append({
            "source":  "finance",
            "content": "No finance files found in the dump folder to process.",
        })
        return ctx

    log.info("finance_process_start", file_count=len(pending), user_id=ctx.user.user_id)

    results, failures = [], []
    for f in pending:
        result = await process_file(str(f))
        if result:
            results.append(result)
        else:
            failures.append(f.name)

    lines = [f"Processed {len(pending)} finance file(s):"]
    for r in results:
        lines.append(
            f"  - {r['friendly_name']} ({r['statement_month']}): "
            f"{r['transactions']} transaction(s) saved"
        )
    if failures:
        lines.append(f"  Failed ({len(failures)}): {', '.join(failures)}")

    total_tx = sum(r["transactions"] for r in results)
    lines.append(f"Total: {total_tx} transaction(s) across {len(results)} account(s)")

    ctx.enrichments.append({
        "source":  "finance",
        "content": "\n".join(lines),
    })

    log.info("finance_process_complete",
             processed=len(results),
             failed=len(failures),
             total_transactions=total_tx,
             user_id=ctx.user.user_id)

    return ctx
