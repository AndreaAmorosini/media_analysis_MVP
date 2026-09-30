from typing import Any

def split_text_by_provenance(item: dict[str, Any]) -> tuple[list[str | None], list[str]]:
    """
    Associa a ciascuna provenance il relativo frammento di item.text.

    Non usa item.orig come fallback: gli offset devono riferirsi
    alla stessa stringa su cui sono stati prodotti.

    Se un elemento multiprovenance non può essere suddiviso
    in modo affidabile, restituisce frammenti None e warning.
    Il testo completo deve essere conservato separatamente dal chiamante.
    """
    
    provenances = item.get("prov") or []
    text = item.get("text")
    
    if not isinstance(text, str):
        return [None] * len(provenances), []
    
    if not provenances:
        return [], ["text_item_without_provenance"]
    
    if len(provenances) == 1:
        span = provenances[0].get("charspan")
        
        if span is None:
            return [text], []
        
    spans: list[tuple[int, int]] = []
    
    for provenance in provenances:
        span = provenance.get("charspan")
        
        if(not isinstance(span, (list, tuple)) or len(span) != 2 or any(type(value) is not int for value in span)):
            return ([None] * len(provenances), ["missing_or_invalid_charspan"])
        
        start, end = span
        
        if not 0 <= start <= end <= len(text):
            return ([None] * len(provenances), ["charspan_out_of_bounds"])
        
        spans.append((start, end))
        
    cursor = 0
    
    for start, end in sorted(spans):
        if start < cursor:
            return ([None] * len(provenances), ["overlapping_charspans"])
        
        if text[cursor:start].strip():
            return ([None] * len(provenances), ["unmapped_non_whitespace_text"])
        
        cursor = end
        
    if text[cursor:].strip():
        return ([None] * len(provenances), ["unmapped_non_whitespace_text"])
    
    return [text[start:end] for start, end in spans], []