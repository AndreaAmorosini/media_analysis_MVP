
from dataclasses import dataclass

import unicodedata


SWAPS = str.maketrans({
    "’": "'",
    "‘": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "−": "-",
})


@dataclass(frozen=True)
class Evidence:
    start: int
    end: int
    quote: str


def folded(text: str):
    parts = []
    starts = []
    ends = []

    for index, char in enumerate(text):

        if char.isspace():
            if parts and parts[-1] == " ":
                ends[-1] = index + 1
            else:
                parts.append(" ")
                starts.append(index)
                ends.append(index + 1)

            continue

        normalized = (unicodedata.normalize("NFKC", char).translate(SWAPS).casefold())

        for mapped in normalized:
            parts.append(mapped)
            starts.append(index)
            ends.append(index + 1)

    return "".join(parts), starts, ends


def locate(snippet: str, offset: int, quote: str):
    quote = (quote or "").strip()

    if len(quote) < 12:
        return None, "short_quote"

    # Prima il confronto esatto.
    if snippet.count(quote) == 1:
        pos = snippet.find(quote)

        return Evidence(start=offset + pos, end=offset + pos + len(quote), quote=quote), "exact"

    # Poi il confronto normalizzato.
    normalized_body, starts, ends = folded(snippet)

    normalized_quote = folded(quote)[0].strip()

    if len(normalized_quote) < 12:
        return None, "short_quote"

    first = normalized_body.find(normalized_quote)

    if first < 0:
        return None, "quote_not_found"

    second = normalized_body.find(normalized_quote, first + 1)

    if second >= 0:
        return None, "quote_ambiguous"

    start = starts[first]

    end = ends[first + len(normalized_quote) - 1]

    return Evidence(start=offset + start, end=offset + end, quote=snippet[start:end]), "normalized"
