from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from press_reputation.config import TextFragmentRecoveryConfig


@dataclass
class RecoveredFragment:
    provenance_index: int
    text: str | None
    raw_charspan: tuple[int, int] | None
    effective_charspan: tuple[int, int] | None
    status: str
    confidence: float
    warnings: list[str] = field(default_factory=list)


@dataclass
class TextFragmentRecoveryResult:
    fragments: list[RecoveredFragment]
    source_text: str | None
    unmapped_intervals: list[tuple[int, int]]
    warnings: list[str] = field(default_factory=list)


class TextFragmentRecovery:
    def __init__(self, config: TextFragmentRecoveryConfig | None = None) -> None:
        self.config = config or TextFragmentRecoveryConfig()

    @staticmethod
    def raw_span(provenance: dict[str, Any]) -> tuple[int, int] | None:
        span = provenance.get("charspan")
        if not isinstance(span, (list, tuple)) or len(span) != 2 or any(type(value) is not int for value in span):
            return None
        return span[0], span[1]

    def alignment_span(self, item: dict[str, Any], text: str, raw_span: tuple[int, int]) -> tuple[int, int] | None:
        orig = item.get("orig")
        if (not isinstance(orig, str) or not orig or len(orig) > self.config.max_alignment_chars
                or len(text) > self.config.max_alignment_chars):
            return None

        matcher = SequenceMatcher(None, orig, text, autojunk=False)
        if matcher.ratio() < self.config.min_alignment_similarity:
            return None

        equal_blocks = [
            (a0, a1, b0)
            for tag, a0, a1, b0, _ in matcher.get_opcodes()
            if tag == "equal"
        ]

        def map_boundary(offset: int) -> int | None:
            mapped = {b0 + offset - a0 for a0, a1, b0 in equal_blocks if a0 <= offset <= a1}
            if offset == len(orig):
                mapped.add(len(text))
            return next(iter(mapped)) if len(mapped) == 1 else None

        start, end = map_boundary(raw_span[0]), map_boundary(raw_span[1])
        if start is None or end is None or not 0 <= start < end <= len(text):
            return None
        return start, end

    @staticmethod
    def unmapped_intervals(text: str, covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []
        cursor = 0

        for start, end in sorted(covered):
            if cursor < start and text[cursor:start].strip():
                result.append((cursor, start))
            cursor = max(cursor, end)

        if cursor < len(text) and text[cursor:].strip():
            result.append((cursor, len(text)))

        return result

    def recover(self, item: dict[str, Any]) -> TextFragmentRecoveryResult:
        provenances = item.get("prov") or []
        text = item.get("text")

        if not isinstance(text, str):
            return TextFragmentRecoveryResult(
                fragments=[
                    RecoveredFragment(i, None, self.raw_span(prov), None, "ambiguous", 0.0, ["source_text_not_string"])
                    for i, prov in enumerate(provenances)
                ],
                source_text=None, unmapped_intervals=[], warnings=["source_text_not_string"],
            )

        if not provenances:
            return TextFragmentRecoveryResult(
                fragments=[], source_text=text,
                unmapped_intervals=[(0, len(text))] if text.strip() else [],
                warnings=["text_item_without_provenance"],
            )

        fragments: list[RecoveredFragment] = []
        length = len(text)

        for index, provenance in enumerate(provenances):
            raw = self.raw_span(provenance)
            effective = None
            status, confidence = "ambiguous", 0.0
            warnings: list[str] = []

            if raw is None:
                if len(provenances) == 1 and text:
                    effective, status, confidence = (0, length), "single_provenance", 0.85
                    warnings.append("missing_charspan_single_provenance")
                else:
                    warnings.append("missing_or_invalid_charspan")
            else:
                start, end = raw

                if 0 <= start < end <= length:
                    effective, status, confidence = raw, "exact", 1.0
                elif (index == len(provenances) - 1 and 0 <= start < length and end > length
                        and end - length <= self.config.max_terminal_overrun):
                    effective, status, confidence = (start, length), "offset_adjusted", 0.90
                    warnings.append("terminal_end_clamped")
                elif start >= 0 and end > start:
                    aligned = self.alignment_span(item, text, raw)
                    if (aligned is not None and max(abs(aligned[0] - start), abs(aligned[1] - end))
                            <= self.config.max_boundary_shift):
                        effective, status, confidence = aligned, "realigned", 0.85
                        warnings.append("offsets_realigned")
                    else:
                        warnings.append("span_not_safely_alignable")
                else:
                    warnings.append("invalid_charspan_bounds")

            fragments.append(RecoveredFragment(
                provenance_index=index,
                text=text[effective[0]:effective[1]] if effective else None,
                raw_charspan=raw, effective_charspan=effective,
                status=status, confidence=confidence, warnings=warnings,
            ))

        occupied: list[tuple[int, int]] = []
        for fragment in sorted(
            fragments,
            key=lambda entry: (
                entry.effective_charspan[0] if entry.effective_charspan else length + 1,
                entry.provenance_index,
            ),
        ):
            span = fragment.effective_charspan
            if span is None:
                continue

            if any(span[0] < end and start < span[1] for start, end in occupied):
                fragment.text, fragment.effective_charspan = None, None
                fragment.status, fragment.confidence = "ambiguous", 0.0
                fragment.warnings.append("overlapping_effective_spans")
            else:
                occupied.append(span)

        unmapped = self.unmapped_intervals(text, occupied)
        warnings = [warning for fragment in fragments for warning in fragment.warnings]
        if unmapped:
            warnings.append("unmapped_non_whitespace_text")

        return TextFragmentRecoveryResult(
            fragments=fragments, source_text=text,
            unmapped_intervals=unmapped, warnings=warnings,
        )
        
        
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