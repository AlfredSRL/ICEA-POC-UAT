"""Deterministic, auditable normalization for DEV comparisons.

Raw source values are never changed here. Callers may display the canonical
value beside the raw value, but must keep the two distinct.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation

WEIGHT_SUFFIXES = {"kg", "kgs", "kilogram", "kilograms", "g", "gr", "gram", "grams",
                   "grammi", "lb", "lbs", "t", "ton", "tonne"}
WEIGHT_PATHS = (".net_weight", ".gross_weight")
DATE_PATTERN = re.compile(r"\b(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2})\b")
CODE_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,4}[-/]?)?\d{3,}(?:[/.-]\d+)*(?![A-Za-z0-9])")
ORIGIN_PATHS = {"tc.raw_materials[].origin_country", "tc.raw_materials[].origin_subdivision"}
SHIPMENT_REFERENCE = "invoice.lines[].shipment_reference"
DELIVERY_POSITION_LABEL = re.compile(r"\blief\.?\s*nr\.?\s*/\s*pos\.?", re.IGNORECASE)
DELIVERY_POSITION_TOKEN = re.compile(r"^\s*(\d{6,})/(\d{4})\s*$")


def is_list_value(path: str) -> bool:
    return path in ORIGIN_PATHS or path in {"invoice.shipment_reference", "invoice.lines[].shipment_reference"} or path.endswith(".lot_numbers[]")


def origin_variants(path: str, value: object) -> set[str]:
    if value is None:
        return set()
    raw = str(value).strip()
    name = re.search(r"\(([^)]+)\)", raw)
    code = re.search(r"\b([A-Z]{2})-([A-Z0-9]+)\b", raw, re.IGNORECASE)
    if path.endswith("origin_country"):
        values = {text(re.split(r"\s*[,(]", raw)[0])}
        if code:
            values.add(code.group(1).casefold())
        return values - {""}
    if path.endswith("origin_subdivision"):
        values = {text(name.group(1))} if name else set()
        if code:
            values.add(text(code.group(0)))
        return values or {text(raw)}
    return set()


def _fold(value: object) -> str:
    value = unicodedata.normalize("NFKC", str(value))
    value = "".join("-" if unicodedata.category(char) == "Pd" or char in "−—–" else char
                    for char in value)
    value = "".join(char for char in unicodedata.normalize("NFKD", value)
                    if unicodedata.category(char) != "Mn")
    return value.casefold()


def text(value: object) -> str:
    """Compare prose independent of separator punctuation, not of its digits."""
    return " ".join(re.sub(r"[^\w\s]", " ", _fold(value)).split())


def identifier(value: object) -> str:
    return "".join(_fold(value).split())


def material_text(value: object) -> str:
    """Small, reviewed material vocabulary; codes and numeric tokens stay exact."""
    words = text(value).split()
    aliases = {"recyled": "recycled", "adhesiv": "adhesive",
               "poliestere": "polyester", "cotone": "cotton"}
    return " ".join(aliases.get(word, word) for word in words)


def _purchase_order_source(value: object) -> str:
    return re.sub(rf"\s+(?:vom|del|dated|du|of)\s+{DATE_PATTERN.pattern}\s*$",
                  "", str(value), flags=re.IGNORECASE)


def _labeled_code(value: object) -> str | None:
    """Remove surrounding prose only when it contains one unambiguous code token."""
    source = DATE_PATTERN.sub("", str(value).strip())
    if (not re.search(r"\s", source)
            or not (re.search(r"[A-Za-zÀ-ÿ]{4,}", source)
                    or re.search(r"\b(?:PO|NO|NR|REF|TC|TE-ID)\b", source, re.IGNORECASE))):
        return None
    tokens = CODE_TOKEN.findall(source)
    return identifier(tokens[0]) if len(tokens) == 1 else None


def date(value: object) -> str | None:
    value = str(value).strip()
    # A printed two-digit year (e.g. 05/10/23) must not be read as year 0023.
    formats = (("%d/%m/%y", "%d.%m.%y", "%d-%m-%y")
               if re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-]\d{2}", value)
               else ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y/%m/%d"))
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def number(value: object) -> tuple[Decimal, str] | None:
    match = re.fullmatch(r"\s*([+-]?[\d.,\s]+)\s*([^\d.,\s]*)\s*", str(value))
    if not match:
        return None
    digits = re.sub(r"\s+", "", match.group(1))
    if "," in digits and "." in digits:
        digits = (digits.replace(".", "").replace(",", ".") if digits.rfind(",") > digits.rfind(".")
                  else digits.replace(",", ""))
    elif "," in digits:
        digits = digits.replace(",", ".")
    try:
        return Decimal(digits), text(match.group(2))
    except InvalidOperation:
        return None


def boolean(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    token = text(value)
    if token in {"true", "vero", "si", "yes", "ja"}:
        return True
    if token in {"false", "falso", "no", "nein"}:
        return False
    return None


def canonical(path: str, rule: str, raw: object, normalized: object = None,
              *, text_evidence: str | None = None):
    """Canonicalize raw independently; use supplied normalization only as fallback."""
    source = raw if raw is not None and str(raw).strip() else normalized
    if source is None:
        return None
    if path in ORIGIN_PATHS:
        values = []
        for entry in str(source).split(";"):
            if path.endswith("origin_country"):
                values.append(text(re.split(r"\s*[,(]", entry.strip())[0]))
            else:
                name = re.search(r"\(([^)]+)\)", entry)
                values.append(text(name.group(1) if name else entry.strip()))
        return "; ".join(values)
    if rule in {"text_normalized", "grouped_value"}:
        if path.endswith(".incoterm") and not text(source) and "-" in _fold(source):
            return "-"
        if path.endswith((".lot_composition", ".material_composition")):
            return material_text(source)
        return text(source)
    if rule == "identifier_exact":
        selected = _purchase_order_source(source) if path.endswith(".purchase_order_number") else source
        # An article number may be descriptive text: keep it whole, never reduce it to a code token.
        value = ((None if path.endswith(".article_number") else _labeled_code(selected))
                 or identifier(selected))
        if path == "transport_document.number":
            value = re.sub(r"^(?:alb\.?n\.?|albaran(?:n[º°.]?|no\.?)?|ddt(?:n[º°.]?|no\.?)?)", "", value)
        return value
    if rule == "date_equivalent":
        return date(source) or (date(normalized) if normalized is not None else None)
    if rule == "numeric_equivalent":
        parsed = number(source)
        if parsed is None and normalized is not None:
            parsed = number(normalized)
        if parsed is None:
            return None
        amount, unit = parsed
        if path.endswith(WEIGHT_PATHS) and unit and unit not in WEIGHT_SUFFIXES:
            return None
        return amount
    if rule == "boolean_judgment":
        supplied = boolean(normalized)
        return supplied if supplied is not None else boolean(raw)
    if rule == "reference_equivalent":
        variants = reference_variants(normalized if normalized is not None else source)
        base = shipment_reference_base(path, raw, text_evidence)
        if base:
            return base
        return min(variants,key=len) if variants else None
    if rule == "presence":
        return bool(str(source).strip())
    return None


def canonical_truth(path: str, rule: str, raw: object, normalized: object = None):
    """Respect an explicit GT normalized value; never apply this to model output."""
    if normalized is not None and str(normalized).strip():
        selected = canonical(path, rule, normalized)
        if selected is not None:
            if rule == "numeric_equivalent":
                original = canonical(path, rule, raw)
                if isinstance(original, Decimal) and isinstance(selected, Decimal):
                    try:
                        if Decimal(str(float(selected))) == original:
                            return original
                    except (OverflowError, ValueError):
                        pass
            return selected
    return canonical(path, rule, raw)


def shipment_reference_base(path: str, raw: object, evidence: str | None) -> str | None:
    """Separate a printed delivery position only with matching column evidence."""
    if path != SHIPMENT_REFERENCE or not evidence or raw is None:
        return None
    token = DELIVERY_POSITION_TOKEN.fullmatch(str(raw))
    if not token or not DELIVERY_POSITION_LABEL.search(evidence):
        return None
    if not re.search(r"(?<!\d)" + re.escape(str(raw).strip()) + r"(?!\d)", evidence):
        return None
    return identifier(token.group(1))


def reference_variants(raw: object, normalized: object = None, *,
                       field_path: str | None = None, text_evidence: str | None = None) -> set[str]:
    """Two GT variants are alternatives, even when they disagree internally."""
    variants = set()
    for value in (raw, normalized):
        if value is None or not str(value).strip():
            continue
        variants.add(identifier(value))
        no_date = DATE_PATTERN.sub("", str(value))
        if DATE_PATTERN.search(str(value)):
            no_date = re.sub(r"\b(?:del|of)\b", "", no_date, flags=re.IGNORECASE)
        no_date = no_date.strip(" -;,/")
        if no_date:
            variants.add(identifier(no_date))
        candidate = _labeled_code(value)
        if candidate:
            variants.add(candidate)
    base = shipment_reference_base(field_path or "", raw, text_evidence)
    if base:
        variants.add(base)
    return variants


# Look-alike glyphs commonly confused by OCR and handwriting readers.
CONFUSABLE = str.maketrans({"o": "0", "i": "1", "l": "1", "s": "5", "b": "8", "z": "2"})
PARTIAL_STOPWORDS = {"srl", "spa", "ltd", "gmbh", "inc", "llc", "pte", "sas", "snc", "the", "and",
                     "per", "del", "della", "dei", "delle", "via", "str", "strasse", "und", "von",
                     "der", "die", "das"}


def _lookalike(value: str) -> str:
    return value.casefold().translate(CONFUSABLE)


def _meaningful_tokens(value: str) -> set[str]:
    return {_lookalike(token) for token in re.findall(r"\w+", value)
            if len(token) >= 3 and token not in PARTIAL_STOPWORDS}


def partial_match(left: object, right: object, rule: str) -> bool:
    """A value is partial when any meaningful part of it matches, however small.

    Covers a contained fragment (e.g. an acronym without its expansion), a shared
    meaningful word or code, look-alike glyphs (O/0, I/1, l/1, S/5, B/8, Z/2) and a
    single-character slip. Dates, numbers and boolean judgments are never partial.
    """
    if rule not in {"identifier_exact", "text_normalized", "grouped_value", "reference_equivalent"}:
        return False
    identifier_like = rule in {"identifier_exact", "reference_equivalent"}
    a = identifier(left) if identifier_like else text(left)
    b = identifier(right) if identifier_like else text(right)
    if not a or not b or a == b:
        return False
    if _lookalike(a) == _lookalike(b):
        return True
    short, long = sorted((a, b), key=len)
    if len(short) >= 3 and (short in long or _lookalike(short) in _lookalike(long)):
        return True
    if _meaningful_tokens(text(left)) & _meaningful_tokens(text(right)):
        return True
    a_words, b_words = a.split(), b.split()
    differences = [(first, second) for first, second in zip(a_words, b_words) if first != second]
    if len(a_words) == len(b_words) and len(differences) == 1:
        first, second = differences[0]
        if len(first) == len(second) and sum(x != y for x, y in zip(first, second)) == 1:
            return True
        shorter, longer = sorted((first, second), key=len)
        if len(longer) - len(shorter) == 1 and any(
                longer[:index] + longer[index + 1:] == shorter for index in range(len(longer))):
            return True
    return False
