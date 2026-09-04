"""TN1's forbidden-language scan, shared by every engine that writes to a person.

`policy-bounds` -> TN is explicit that tone constraints "apply to **every**
customer-facing message any engine generates: Engine 2's dunning, Engine 3's
reminders, and any notification body". They arrived in Engine 2 and lived in
`engines/mandate_recovery/dunning.py` because that was the only caller.

Engine 3 is the second caller, and the choice was between importing one engine's
module from another or keeping a second copy of the list. Both are wrong for the
same reason — a bound with two definitions has two values the moment either one
is edited — so TN1's patterns moved here, into the shared core, where the rule
they enforce already lives (`policy_engine.RULE_REGISTRY["policy-bounds:TN1"]`).

**TN2 stays with each engine.** "A remedy the customer can actually complete" is
a claim about that engine's failure modes: Engine 2's permitted calls to action
are about payment instruments, Engine 3's are about invoices. There is nothing
shared to factor out, and forcing one would produce a union that permits both
engines to say things neither should.

Known limitation, stated rather than discovered later: **these patterns are
English.** A Hinglish or vernacular channel would pass a threat written in Hindi
straight through. Engine 3 *reads* Hinglish and only writes English, so nothing
in this repository is exposed today — but a vernacular outreach channel would
need this list extended before it shipped, not after.
"""

from __future__ import annotations

import re

#: `policy-bounds:TN1`. Phrases asserting a consequence Vasooli will not carry
#: out, or a deadline no rule imposes. Matched case-insensitively on word
#: boundaries. The list is deliberately concrete rather than clever: a fuzzy
#: "sounds threatening" check would fail unpredictably, and an unpredictable gate
#: is not a gate.
FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\blegal action\b", "threatens legal action"),
    (r"\blawsuit\b|\bsue\b|\bsued\b", "threatens litigation"),
    (r"\bcollection(s)? agency\b|\bdebt collector\b", "threatens collections"),
    (r"\bcredit (score|bureau|report)\b|\bcibil\b", "threatens credit reporting"),
    (r"\bblacklist(ed)?\b", "threatens blacklisting"),
    (r"\bpenalt(y|ies)\b|\blate fee(s)?\b|\bfine\b", "asserts a charge that does not exist"),
    (r"\bsuspend(ed)?\b|\bterminat(e|ed|ion)\b|\bdeactivat(e|ed)\b", "threatens service loss"),
    (r"\bfinal (notice|warning|reminder)\b", "manufactures a terminal deadline"),
    (r"\bimmediately\b|\bact now\b|\burgent(ly)?\b", "manufactures urgency"),
    (r"\bwithin 24 hours\b|\blast chance\b", "manufactures a deadline"),
)


def scan_forbidden(text: str) -> list[str]:
    """Every TN1 violation in `text`, as readable detail strings.

    Returns all of them rather than the first, so an audit entry can say what was
    wrong with a message instead of only that something was.
    """
    found: list[str] = []
    for pattern, detail in FORBIDDEN_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            found.append(f"{detail} ({match.group(0)!r})")
    return found


__all__ = ["FORBIDDEN_PATTERNS", "scan_forbidden"]
