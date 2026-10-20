"""Pulizia conservativa del testo del titolo: rumore OCR e codici di registro.

Non modifica il testo raw delle regioni né la provenance: si applica solo al
testo del titolo assegnato al draft/record e registra le azioni eseguite.

Le tre regole:
1. token identici consecutivi ("DS1667 DS1667") ridotti a una sola occorrenza;
2. codici di registro stile watermark/seriale (es. "DS1667", "2024001234")
   rimossi dal titolo;
3. correzione OCR di un toponimo solo se il token è a distanza di edit 1 da un
   comune italiano che risulta attestato altrove nel testo del documento
   (un lessico vuoto lascia la parola invariata).
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

DUPLICATED_TOKEN = re.compile(r"(?<![\w-])(\w+)(?:\s+\1)(?![\w-])", re.IGNORECASE)
REGISTRATION_CODE = re.compile(r"(?<![\w-])(?:[A-Z]{1,4}\d{3,}|\d{4,})(?![\w-])")
# Codici di registro FUSI dall'OCR dentro la headline. L'OCR sovrappone il
# watermark della rassegna al testo del titolo e produce quattro varianti:
#   1. prefisso lettere+cifre seguito da maiuscola ("DS16Tornano");
#   2. cifre spurious in testa a una parola capitalizzata ("6Imprenditore");
#   3. codice in coda alla parola ("ImprenditoreS1667");
#   4. sequenza di cifre incuneata dentro la parola ("FOSSOIMPERATORE667RESIDENTI").
FUSED_REGISTRATION_CODE = re.compile(r"\b[A-Z]{1,4}\d{2,6}(?=[A-Z])")
LEADING_STRAY_DIGITS = re.compile(r"\b\d{1,4}(?=[A-Z][a-zà-ù])")
# Il lookbehind in minuscolo evita di scortecciare i codici standalone
# ("DS1667"): quelli li rimuove REGISTRATION_CODE. Qui si interviene solo
# sulle fusioni a-meta'-parola ("ImprenditoreS1667").
TRAILING_FUSED_CODE = re.compile(r"(?<=[a-zà-ù])[A-Z]{1,4}\d{3,6}\b")
EMBEDDED_FUSED_CODE = re.compile(r"(?<=[A-Za-zà-ù])\d{3,}(?=[A-Za-zà-ù])")
# Cifre di watermark incuneate dopo la virgola ("fognaria,67 rush"): in un
# titolo editoriale le cifre dopo virgola sono sempre staccate da uno spazio.
FUSED_COMMA_DIGITS = re.compile(r"(?<=,)\d{2,4}(?=\s)")
MIN_TOKEN_CHARS = 4


def unwrap_fused_codes(text: str) -> str:
    text = LEADING_STRAY_DIGITS.sub("", text)
    text = FUSED_REGISTRATION_CODE.sub("", text)
    text = TRAILING_FUSED_CODE.sub(" ", text)
    text = EMBEDDED_FUSED_CODE.sub(" ", text)
    text = FUSED_COMMA_DIGITS.sub("", text)
    return text


def _normalize_word(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in value if not unicodedata.combining(char))


@lru_cache(maxsize=1)
def _italian_comuni() -> dict[str, str]:
    path = Path(__file__).resolve().parents[1] / "resources" / "comuni_italiani.json"
    result: dict[str, str] = {}
    for item in json.loads(path.read_text(encoding="utf-8")):
        name = item.get("comune")
        if name:
            result[_normalize_word(name)] = name
    return result


def _levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def _ocr_variants(word: str) -> set[str]:
    """Varianti canoniche delle confusioni OCR più frequenti."""
    return {
        word,
        word.replace("m", "rn"),
        word.replace("rn", "m"),
        word.replace("ii", "n"),
        word.replace("n", "ii"),
    }


def clean_article_title(text: str, document_text: str | None = None) -> tuple[str, list[str]]:
    """Ritorna (titolo pulito, azioni registrate)."""
    actions: list[str] = []
    title = (text or "").strip()
    if not title:
        return title, actions

    deduplicated = DUPLICATED_TOKEN.sub(r"\1", title)
    if deduplicated != title:
        actions.append("dedupe_repeated_token")
        title = deduplicated

    unfused = unwrap_fused_codes(title)
    if unfused != title:
        actions.append("unwrap_fused_registration_code")
        title = unfused

    stripped = REGISTRATION_CODE.sub(" ", title)
    if stripped.strip() != title.strip():
        actions.append("strip_registration_code")
    title = re.sub(r"\s+", " ", stripped).strip()

    if document_text:
        attested = {_normalize_word(word) for word in re.findall(r"\w+", document_text)}
        comuni = _italian_comuni()
        corrected = []
        last_end = 0
        for match in re.finditer(r"\w+", title):
            corrected.append(title[last_end:match.start()])
            token = match.group(0)
            normalized = _normalize_word(token)
            replacement = None
            # Un token attestato altrove nel documento non si tocca. Un token
            # non attestato si corregge solo verso un comune attestato e
            # vicino (confusione OCR canonica o distanza 1). L'azione è
            # sempre registrata per la revisione.
            if len(normalized) >= MIN_TOKEN_CHARS and normalized not in attested:
                variants = _ocr_variants(normalized)
                for candidate in sorted(comuni):
                    if len(candidate) < MIN_TOKEN_CHARS:
                        continue
                    # Confusione OCR canonica con match esatto e attestazione,
                    # oppure distanza di edit 1 con attestazione.
                    if (candidate in variants or
                            (abs(len(candidate) - len(normalized)) <= 1 and
                             _levenshtein(normalized, candidate) == 1)) and candidate in attested:
                        replacement = comuni[candidate]
                        break
            if replacement:
                actions.append(f"corrected_ocr_comune:{token}->{replacement}")
                corrected.append(replacement)
            else:
                corrected.append(token)
            last_end = match.end()
        corrected.append(title[last_end:])
        title = "".join(corrected)

    if not title:
        return (text or "").strip(), actions
    return title, actions
