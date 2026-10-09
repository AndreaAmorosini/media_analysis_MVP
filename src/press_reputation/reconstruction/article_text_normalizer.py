import re
import unicodedata

from press_reputation.config import ArticleTextNormalizationConfig
from press_reputation.models.page import PageRecord, RegionType
from press_reputation.reconstruction.flow_models import ArticleDraft, DraftSegment


class ArticleTextNormalizer:
    HYPHEN_END = re.compile(r"([^\W\d_]{2,})([-\u2010\u00ad])$", re.UNICODE)
    NEXT_WORD = re.compile(r"^([^\W\d_]+)(.*)$", re.UNICODE)
    PROTECTED = re.compile(
        r"https?://\S+|www\.\S+|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|"
        r"\b\d+[.,]\d+\b|\b(?:[A-Z]\.){2,}"
    )

    LIGATURES = str.maketrans({
        "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    })
    QUOTES = str.maketrans({
        "“": '"', "”": '"', "„": '"', "‟": '"',
        "‘": "'", "’": "'",
    })
    COMMON_MOJIBAKE = {
        "â€™": "'", "â€œ": '"', "â€\x9d": '"',
        "Ã¨": "è", "Ã©": "é", "Ã ": "à",
    }

    def __init__(self, config: ArticleTextNormalizationConfig | None = None) -> None:
        self.config = config or ArticleTextNormalizationConfig()

    def record(self, actions: list[dict], kind: str, segment: DraftSegment | None, **data) -> None:
        if len(actions) >= self.config.max_recorded_actions:
            return

        actions.append({
            "kind": kind,
            "region_id": segment.region_id if segment else None,
            "pdf_page": segment.pdf_page if segment else None,
            "raw_article_charspan": segment.article_charspan if segment else None,
            **data,
        })

    def unicode_cleanup(self, text: str, segment: DraftSegment | None, actions: list[dict]) -> str:
        original = text
        text = unicodedata.normalize("NFC", text).translate(self.LIGATURES)
        text = text.replace("\u00a0", " ").replace("\u202f", " ")

        # Soft hyphen interno a una parola: non è un trattino lessicale.
        text = re.sub(r"(?<=[^\W\d_])\u00ad(?=[^\W\d_])", "", text)
        text = re.sub(r"(?<=[^\W\d_])\u200b(?=[^\W\d_])", "", text)
        text = text.replace("\ufeff", "")

        for corrupted, replacement in self.COMMON_MOJIBAKE.items():
            text = text.replace(corrupted, replacement)

        text = text.translate(self.QUOTES)
        if text != original:
            self.record(actions, "unicode_or_quote_cleanup", segment)

        if "\ufffd" in text or re.search(r"Ã.|â€", text):
            self.record(actions, "unresolved_unicode_corruption", segment)

        return text

    def punctuation_cleanup(self, text: str) -> str:
        protected: list[str] = []

        def hold(match: re.Match) -> str:
            protected.append(match.group())
            return f"\ue000{len(protected) - 1}\ue001"

        text = self.PROTECTED.sub(hold, text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\s+([,.;:!?])", r"\1", text)
        text = re.sub(r"([,;:!?])(?=[^\s\d,;:!?\"'»)\]])", r"\1 ", text)
        text = re.sub(r"(?<=[a-zà-ÿ])\.(?=[A-ZÀ-Ý])", ". ", text)
        text = re.sub(r"\(\s+", "(", text)
        text = re.sub(r"\s+\)", ")", text)

        for index, value in enumerate(protected):
            text = text.replace(f"\ue000{index}\ue001", value)

        return text.strip()

    @staticmethod
    def observed_words(draft: ArticleDraft) -> set[str]:
        text = " ".join([draft.title or "", *(segment.text for segment in draft.segments)])
        return set(re.findall(r"[^\W\d_]+", text.casefold(), flags=re.UNICODE))

    def join_text(self, left: str, right: str, segment: DraftSegment, observed: set[str], actions: list[dict]) -> str:
        left = left.rstrip()
        right = right.lstrip()
        ending = self.HYPHEN_END.search(left)
        beginning = self.NEXT_WORD.match(right)

        if ending and beginning and beginning.group(1)[0].islower():
            stem, hyphen = ending.groups()
            suffix = beginning.group(1)
            joined = (stem + suffix).casefold()
            accepted = {word.casefold() for word in self.config.accepted_joined_words}

            if hyphen == "\u00ad" or joined in observed or joined in accepted:
                self.record(
                    actions, "dehyphenated", segment,
                    candidate=stem + suffix,
                    reason="soft_hyphen" if hyphen == "\u00ad" else "attested_joined_word",
                )
                return left[:-1] + right

            # Ambiguo: togliere il line break, NON il trattino.
            self.record(actions, "hyphen_preserved", segment, candidate=stem + "-" + suffix)
            return left[:-1] + "-" + right

        return f"{left} {right}"

    @staticmethod
    def accepted_cross_page(draft: ArticleDraft, left: DraftSegment, right: DraftSegment) -> bool:
        return any(
            link.status == "accepted" and link.article_candidate_id == draft.id
            and link.from_pdf_page == left.pdf_page and link.to_pdf_page == right.pdf_page
            for link in draft.links
        )

    def join_segments(self, draft: ArticleDraft, left: DraftSegment | None, right: DraftSegment, current: str, next_line: str) -> bool:
        if left is None or left.type != RegionType.ARTICLE_BODY:
            return False

        if left.pdf_page != right.pdf_page:
            return (
                self.accepted_cross_page(draft, left, right)
                and (
                    current.rstrip().endswith(("-", "\u2010", "\u00ad"))
                    or (not current.rstrip().endswith((".", "!", "?", "…", ":", ";")) and bool(next_line) and next_line[0].islower())
                )
            )

        if left.column != right.column or left.column is None:
            return (
                not current.rstrip().endswith((".", "!", "?", "…", ":", ";"))
                and bool(next_line)
                and next_line[0].islower()
            )

        if not left.bbox or not right.bbox:
            return False

        gap = right.bbox[1] - left.bbox[3]
        return -4.0 <= gap <= self.config.max_same_column_gap

    def drop_cap(self, left: DraftSegment | None, right: DraftSegment, current: str, next_line: str, regions: dict[tuple[int, str], object]) -> bool:
        if (left is None or not re.fullmatch(r"[A-ZÀ-Ý]", current.strip()) or not next_line or not next_line[0].islower() or 
            left.pdf_page != right.pdf_page or left.column is None or left.column != right.column or not left.bbox or not right.bbox):
            return False

        first = regions.get((left.pdf_page, left.region_id))
        second = regions.get((right.pdf_page, right.region_id))
        large = first.style.get("max_font_size") if first else None
        normal = second.style.get("median_font_size") if second else None

        return bool(
            large and normal and large >= normal * self.config.min_dropcap_font_ratio
            and 0 <= right.bbox[0] - left.bbox[2] <= self.config.max_dropcap_horizontal_gap
            and right.bbox[1] < left.bbox[3] and right.bbox[3] > left.bbox[1]
        )

    def normalize(self, draft: ArticleDraft, regions: dict[tuple[int, str], object]) -> None:
        draft.body_raw = draft.body_raw if draft.body_raw is not None else draft.body
        actions: list[dict] = []
        observed = self.observed_words(draft)
        paragraphs: list[str] = []
        current = ""
        previous: DraftSegment | None = None

        def flush() -> None:
            nonlocal current
            if current.strip():
                paragraphs.append(self.punctuation_cleanup(current))
            current = ""

        for segment in sorted(draft.segments, key=lambda item: item.order if item.order is not None else 10**9):
            cleaned = self.unicode_cleanup(segment.text, segment, actions)
            lines = cleaned.splitlines() or [cleaned]

            if segment.type == RegionType.ARTICLE_SECTION_HEADER:
                flush()
                heading = self.punctuation_cleanup(" ".join(line.strip() for line in lines if line.strip()))
                if heading:
                    paragraphs.append(heading)
                previous = None
                continue

            for index, raw_line in enumerate(lines):
                if not raw_line.strip():
                    flush()
                    previous = None
                    continue

                indented = (index > 0 and len(raw_line) - len(raw_line.lstrip(" ")) >= self.config.min_paragraph_indent_spaces)
                line = raw_line.strip()

                if not current:
                    current = line
                elif indented:
                    flush()
                    current = line
                elif index > 0:
                    current = self.join_text(current, line, segment, observed, actions)
                elif self.join_segments(draft, previous, segment, current, line):
                    if self.drop_cap(previous, segment, current, line, regions):
                        current += line
                        self.record(actions, "drop_cap_joined", segment, previous_region_id=previous.region_id)
                    else:
                        current = self.join_text(current, line, segment, observed, actions)
                else:
                    flush()
                    current = line

            previous = segment

        flush()
        draft.body_clean = "\n\n".join(paragraphs)
        draft.text_normalization = {
            "method": "article_text_normalizer_v1",
            "body_raw_chars": len(draft.body_raw),
            "body_clean_chars": len(draft.body_clean),
            "actions": actions,
            "actions_truncated": len(actions) >= self.config.max_recorded_actions,
            "charspan_scope": "body_raw",
        }

    def enrich(self, drafts: list[ArticleDraft], pages: list[PageRecord]) -> list[ArticleDraft]:
        regions = {(page.pdf_page, region.metadata["region_id"]): region for page in pages for region in page.regions 
                    if region.metadata.get("region_id")}
        for draft in drafts:
            self.normalize(draft, regions)
        return drafts