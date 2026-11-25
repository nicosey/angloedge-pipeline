"""
Routine RNS filings — the single source of truth for both repos.

Used by:
  db.get_unenriched_rns      routine filings are never sent for enrichment
  export_companies.py        each exported announcement carries "routine_type",
                             which the site uses to group routine filings
                             (the site holds no patterns of its own)

routine_type(headline, category) returns a display label, or None for
announcements the model should judge.

Precedence: a substantive collector category wins over a routine match, so
combined headlines like "Placing … and TVR" or "Final Results and TVR" are still
enriched. The one exception: a Share Buyback row that is really a Transaction in
Own Shares (daily buyback execution) stays routine, and Third-party Research
(Kepler, QuotedData, Hardman & Co) is routine whatever the category.

Labels are shown on the site ("12 Holding(s) in Company · 5 Total Voting Rights"),
so rename with care. Patterns are matched case-insensitively against the stored
headline, which may be truncated at ~50 characters.
"""
import re

_R = lambda p: re.compile(p, re.I)

# First match wins.
ROUTINE_TYPES = [
    ("Transaction in Own Shares", _R(
        r"transactions?\s+in\s+own\s+shares|transactions?\s+in\s+ow(n\s*s?h?)?\.\.\.$"   # incl. truncated
        r"|\bpurchase\s+of\s+own\s+(ordinary\s+)?shares\b"
        r"|\bdaily\s+buy-?\s?back\s+notification"
        r"|share\s+(buy-?\s?back|repurchase)\s+programme\s*:\s*transactions"
        r"|periodic\s+report\s+on\s+the\s+buy-?\s?back"
        r"|^\s*eqs-cms\b")),
    ("Holding(s) in Company", _R(
        r"holdings?\(?s?\)?\s+in\s+company"
        r"|notification\s+of\s+major\s+(holdings?|interests?)"
        r"|\btr-?\s?1\b"
        r"|^\s*eqs-pvr\b"
        r"|standard\s+form\s+for\s+notification")),
    ("Total Voting Rights", _R(r"total\s+voting\s+rights|\btvr\b")),
    ("Form 8 Disclosure", _R(
        r"^\s*(irish\s*)?form\s*-?\s*8\b"
        r"|\bform\s*8\b(?!\s*-?\s*k\b)"            # anywhere, but not a US Form 8-K
        r"|opening\s+position\s+disclosure|\(dealing\s+disclosure\)"
        r"|form\s*8\.[35]"
        r"|form\s*8\s*\((dd|opd)\)"
        r"|irish\s+takeover\s+panel"
        r"|_ie_[a-z]+"
        r"|rule\s*2\.(9|10)\b")),          # securities in issue; Rule 2.8 is substantive
    ("Form 38.5", _R(r"(rule|forms?)\s*-?\s*38\.5")),
    ("Final Terms", _R(r"final\s+terms|pricing\s+supplement")),
    ("Disclosure Table", _R(r"disclosure\s+table")),
    ("Gearing Disclosure", _R(r"\bgearing\s+(announcement|disclosures?)")),
    ("Portfolio Disclosure", _R(
        r"monthly\s+fact\s?sheet"
        r"|^\s*(publication\s+of\s+)?(monthly\s+)?fact\s?sheet"
        r"|\b(top|ten)\s+(\d+|ten|largest)\s+(equity\s+)?(holdings|investments)"
        r"|^\s*portfolio\s+disclosure"
        r"|disclosure\s+of\s+portfolio\s+holdings"
        r"|^\s*quarterly\s+disclosure")),
    ("Block Listing / Admission", _R(
        r"block\s*listing|block\s+admission"
        r"|^\s*admission\s+to\s+trading\s*(\(.*)?$"
        r"|admission\s+of\s+further\s+(securities|shares)"
        r"|^\s*additional\s+listing"
        r"|^\s*admission\s+of\s+shares\b"
        r"|^\s*issue\s+of\s+(equity|shares)\b(?!.*(placing|fundrais|acqui))")),
    ("Treasury Shares", _R(
        r"(sale|transfer|re-?issue|cancellation)\s+of\s+(shares\s+from\s+)?treasury"
        r"|shares\s+from\s+treasury|treasury\s+shares")),
    ("Employee Share Schemes", _R(
        r"\bebt\b|employee\s+benefit\s+trust|share\s+trusts|long\s+term\s+incentive\s+plans?"
        r"|^\s*grant\s+of\s+(share\s+)?options"
        r"|^\s*exercise\s+of\s+(share\s+)?(options|warrants)")),
    ("Director Declaration", _R(r"^\s*director\s+declaration")),
    # "Issue of Debt" is deliberately absent: for a small cap it is material.
    # Programme mechanics (MTN/EMTN programmes, admission particulars, base
    # prospectuses, offering circulars) are routine: banks, sovereigns, housing
    # associations and bond SPVs, not news for shareholders.
    ("Debt Notices", _R(
        r"stabilisation\s+notice|early\s+redemption|notice\s+of\s+redemption"
        r"|notice\s+to\s+(note|bond)holders|supplementary\s+prospectus"
        r"|admission\s+particulars|information\s+memorandum|offering\s+circular"
        r"|medium[\s-]+term\s+notes?|\bemtn\b|\b(note|bond)\s+programme"
        r"|\bbase\s+prospectus")),
    # Notices only — meeting *results* go to the model (a shareholder revolt is news)
    ("Meeting Notices", _R(
        r"^\s*(notice\s+of\s+(the\s+)?(agm|egm|gm|general\s+meeting|annual\s+general\s+meeting"
        r"|extraordinary\s+general\s+meeting)|posting\s+of\s+(circular\s+and\s+)?notice)"
        r"|^\s*form\s+of\s+proxy")),
    ("Administrative", _R(
        r"compliance\s+with\s+market\s+abuse|closed\s+period|change\s+of\s+registered\s+office"
        r"|cdi\s+monthly\s+movement|report\s+on\s+payments\s+to\s+governments"
        r"|^\s*(notice\s+of\s+)?investor\s+presentation|investor\s+meet\s+company")),
    ("Third-party Research", _R(r"kepler\s+trust\s+intelligence|\bquoteddata\b|\bhardman\s*&\s*co\b")),
]

# Collector categories that win over a routine match (the substantive ones).
SUBSTANTIVE_CATEGORIES = frozenset({
    "Results", "Acquisition", "Trading Update", "Placing", "Director Dealing",
    "Share Buyback", "New Listing", "Appointment",
})

# (category, routine label) pairs where the routine match wins anyway.
_ROUTINE_WINS = {("Share Buyback", "Transaction in Own Shares")}
# Labels that win over any category: third-party research about a company's
# results or deals is still someone else's note, not the company's news.
_ALWAYS_ROUTINE = {"Third-party Research"}


def match_routine(headline):
    """The first routine label whose pattern matches the headline, ignoring category."""
    for label, pattern in ROUTINE_TYPES:
        if pattern.search(headline or ""):
            return label
    return None


def routine_type(headline, category=None):
    """Routine label for an announcement, or None if the model should judge it."""
    label = match_routine(headline)
    if (label and category in SUBSTANTIVE_CATEGORIES and label not in _ALWAYS_ROUTINE
            and (category, label) not in _ROUTINE_WINS):
        return None
    return label


def is_routine(headline, category=None):
    return routine_type(headline, category) is not None
