"""Pulizia del testo OCR dai residui di watermark/registro di stampa.

I provider di rassegna (Data Stampa) applicano watermark e codici di
registrazione (es. DS1667) che l'OCR, quando la colonna del watermark
sovrapposta al testo, fonde nei token del body ("futuroDS1667 con").
Anche la dicitura di copyright finisce fusa in coda ai paragrafi.

Questo passaggio, eseguito sulle pagine dopo il merge OCR/PDF:
  1. raccoglie i "token di registro" attestati fuori dal body (watermark,
     diritti, intestazioni tecniche): DS1667, S.12501, SL_CAM, ...;
  2. rimuove dai testi body le occorrenze di quei token, anche fuse
     dentro parole ("futuroDS1667 con" → "futuro con");
  3. rimuove le varianti di "riproduzione riservata" quando appaiono
     fuse nel testo.

Ogni azione è registrata in region.metadata["watermark_scrub"] per la
revisione; il testo raw resta nei JSON raw e nella provenance.
"""
from __future__ import annotations

import re
from collections import defaultdict

from press_reputation.models.page import PageRecord, Region, RegionType

CODE_TOKEN = re.compile(r"\b(?P<code>[A-Z]{1,4}\.?[A-Z]?\.?\d{3,8})\b")
# Varianti della dicitura di copyright: pulita ("riproduzione riservata"),
# prefissata ("@riproduzione riservata", "© RIPRODUZIONE RISERVATA"), fusa
# dall'OCR senza spazi ("riproduzioneriservata") o con underscore.
RIGHTS_PHRASE = re.compile(
    r"[\s((@©•_]*riproduzione[\s_]*riservata[\s).]*",
    re.IGNORECASE,
)
MIN_CODE_LEN = 4

# Regioni editoriali soggette a scrub: il body (residui fusi dal watermark),
# i titoli (i codici fusi nella headline falsano la similarita' con l'indice)
# e le regioni unknown che la ricostruzione puo' assorbire nei body.
SCRUBBED_TYPES = {
    RegionType.ARTICLE_BODY,
    RegionType.ARTICLE_TITLE,
    RegionType.UNKNOWN,
}


def _candidate_tokens(pages: list[PageRecord]) -> dict[str, int]:
    """Token di registro attestati in regioni non editoriali del documento."""
    tokens: defaultdict[str, int] = defaultdict(int)
    trusted_types = {
        RegionType.WATERMARK,
        RegionType.RIGHTS_NOTICE,
        RegionType.HEADER_METADATA,
        RegionType.FOOTER,
    }
    for page in pages:
        for region in page.regions:
            if region.type not in trusted_types or not region.text:
                continue
            for match in CODE_TOKEN.finditer(region.text):
                code = match.group("code")
                if len(code) >= MIN_CODE_LEN and not code.isdigit():
                    tokens[code] += 1
    return dict(tokens)


def _scrub_text(text: str, tokens: dict[str, int]) -> tuple[str, list[dict]]:
    actions: list[dict] = []

    for code in sorted(tokens, key=len, reverse=True):
        pattern = re.compile(r"(?<!\w)" + re.escape(code) + r"(?!\w)|(?<=[a-zà-ù])" + re.escape(code) + r"(?=\s|$)")
        cleaned, count = pattern.subn(" ", text)
        if count:
            actions.append({"action": "remove_registration_code", "token": code, "count": count})
            text = cleaned

    cleaned, count = RIGHTS_PHRASE.subn(" ", text)
    if count:
        actions.append({"action": "remove_rights_phrase", "count": count})
        text = cleaned

    # La rimozione puo' lasciare spazi multipli.
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text, actions


def scrub_watermark_residue(pages: list[PageRecord]) -> None:
    """Pulisce i residui di watermark dai body OCR di tutto il documento."""
    tokens = _candidate_tokens(pages)
    if not tokens:
        # Senza token attestati la frase dei diritti resta comunque pulita.
        pass

    for page in pages:
        for region in page.regions:
            if region.type not in SCRUBBED_TYPES or not region.text:
                continue
            cleaned, actions = _scrub_text(region.text, tokens)
            # Un titolo non va mai svuotato; per body e regioni unknown la
            # rimozione puo' lasciare la regione vuota (es. regione che
            # conteneva solo "@riproduzione riservata") ed e' corretto.
            if actions and cleaned != region.text and (cleaned.strip() or region.type != RegionType.ARTICLE_TITLE):
                region.metadata["watermark_scrub"] = actions
                region.text = cleaned


def scrub_rights_phrase(text: str) -> str:
    """Rimuove dal testo le sole diciture di copyright.

    Usata sui body dei record quando il testo arriva da fonti che lo scrub
    delle regioni non vede (estrazione web con trafilatura, frammenti
    assorbiti in ricostruzione): una dicitura "riproduzione riservata" in
    un body non e' mai testo editoriale.
    """
    if not text:
        return text
    cleaned = RIGHTS_PHRASE.sub(" ", text)
    if cleaned != text:
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r" *\n *", "\n", cleaned).strip()
    return cleaned
