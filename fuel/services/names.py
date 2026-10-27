"""Place-name normalisation shared by the gazetteer build, the station import and request resolution.

Census, GeoNames, the OPIS CSV and people typing into Postman all spell places differently
("St. Louis" / "Saint Louis", "LaGrange" / "La Grange", "Nashville-Davidson metropolitan government
(balance)"). Everything is compared through `place_key`.
"""

import re
import unicodedata

US_STATES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}
STATE_BY_NAME = {name.lower(): code for code, name in US_STATES.items()}

# Census appends the legal/statistical area description to every NAME; strip it (longest first).
_CENSUS_SUFFIXES = sorted(
    [
        "city and borough",
        "metropolitan government",
        "metro government",
        "unified government",
        "consolidated government",
        "urban county",
        "charter township",
        "municipality",
        "township",
        "borough",
        "village",
        "city",
        "town",
        "cdp",
        "ccd",
        "plantation",
        "gore",
        "grant",
        "location",
        "purchase",
        "reservation",
        "unorganized territory",
        "comunidad",
        "zona urbana",
        "corporation",
    ],
    key=len,
    reverse=True,
)
_TOKEN_MAP = {
    "st": "saint",
    "ste": "sainte",
    "ft": "fort",
    "mt": "mount",
    "pt": "point",
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
}


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def strip_census_suffix(name: str) -> str:
    name = re.sub(r"\s*\(balance\)\s*$", "", name.strip(), flags=re.I)
    lowered = name.lower()
    for suffix in _CENSUS_SUFFIXES:
        if lowered.endswith(" " + suffix):
            return name[: -len(suffix) - 1].strip()
    return name


def place_key(name: str) -> str:
    """'St. Louis' -> 'saintlouis'; 'La Grange' == 'LaGrange'; "O'Neill" -> 'oneill'."""
    text = _ascii(name).lower().replace("&", " and ")
    tokens = re.findall(r"[a-z0-9]+", text)
    return "".join(_TOKEN_MAP.get(t, t) for t in tokens)


def census_aliases(raw_name: str) -> list[str]:
    """Display names a Census NAME should be findable by (e.g. 'Nashville-Davidson ...' -> also 'Nashville')."""
    base = strip_census_suffix(raw_name)
    # Keep the unstripped name too: a few NAMEs end in a real word ("Carson City").
    names = [base, re.sub(r"\s*\(balance\)\s*$", "", raw_name.strip(), flags=re.I)]
    for sep in ("-", "/"):
        if sep in base:
            names.append(base.split(sep)[0].strip())
    return [n for n in dict.fromkeys(names) if n]


def state_code(text: str) -> str | None:
    text = text.strip().strip(".").strip()
    if text.upper() in US_STATES:
        return text.upper()
    return STATE_BY_NAME.get(text.lower())
