from __future__ import annotations

import re
from press_reputation.models.page import PageRecord, RegionType


class EditorialFragmentRefinementResolver:
    """Recovers only UNKNOWN fragments enclosed by coherent assigned body.

    Does not relabel known section headings or replace OCR text.
    """
    BLOCKED = {"related", "advertisement", "boilerplate", "non_main", "navigation"}
    UI = re.compile(r"^(south|chevron_(?:right|left)|menu|oggi|next article)$", re.I)

    @staticmethod
    def overlap(a, b):
        return max(0, min(a[2], b[2]) - max(a[0], b[0])) / max(1, min(a[2]-a[0], b[2]-b[0]))

    def interpolated_column_owner(self, fragment: Region, bodies: list[Region]) -> list[str]:
        """Owner per un frammento in una colonna senza body assegnati sopra/sotto.

        Colonna interpolata: il frammento e' adiacente in orizzontale a una
        colonna gia' posseduta da un articolo (a destra o a sinistra, scarto
        minimo), con la sua fascia verticale dentro l'estensione dell'articolo
        sulla pagina. Nessun altro articolo deve possedere body nella stessa
        fascia x: in quel caso il frammento resta orfano. E' il caso delle
        colonne mai "possedute" perche' l'OCR le ha frammentate in unknown
        (es. colonna 3 tra la 2 e la 4 di un clipping).
        """
        x_gap = 18.0
        by_owner: dict[str, set[str]] = {}
        for body in bodies:
            side = None
            if 0 < fragment.bbox[0] - body.bbox[2] <= x_gap:
                side = "left"
            elif 0 < body.bbox[0] - fragment.bbox[2] <= x_gap:
                side = "right"
            if side:
                by_owner.setdefault(body.article_id, set()).add(side)
        candidates = list(by_owner)
        if not candidates or len(candidates) != 1:
            return []
        owner = candidates[0]
        # Nessun articolo (nemmeno l'owner) ha body che attraversano la fascia
        # orizzontale del frammento NELLA SUA STESSA FASCIA VERTICALE: un lead
        # a tutta larghezza sopra le colonne non e' un concorrente della
        # colonna sotto di lui.
        for body in bodies:
            if body.bbox[3] <= fragment.bbox[1] or body.bbox[1] >= fragment.bbox[3]:
                continue
            if (body.bbox[0] < fragment.bbox[2] - 5
                    and body.bbox[2] > fragment.bbox[0] + 5):
                return []
        owner_boxes = [b.bbox for b in bodies if b.article_id == owner]
        if not owner_boxes:
            return []
        top = min(b[1] for b in owner_boxes)
        bottom = max(b[3] for b in owner_boxes)
        if not (top - 35 <= (fragment.bbox[1] + fragment.bbox[3]) / 2 <= bottom + 35):
            return []
        return [owner]

    def enrich_page(self, page: PageRecord) -> list[dict]:
        report = []
        bodies = [r for r in page.regions if r.type == RegionType.ARTICLE_BODY and r.article_id
                    and r.bbox and not r.exclude_from_article_text]
        for fragment in page.regions:
            if (fragment.type != RegionType.UNKNOWN or not fragment.text or not fragment.bbox
                or fragment.article_id or fragment.exclude_from_article_text
                or fragment.metadata.get("content_scope") in self.BLOCKED
                or fragment.metadata.get("in_header_metadata_zone")
                or self.UI.fullmatch(fragment.text.strip())):
                continue
            # Never treat Docling's heading as body on proximity alone.
            if fragment.raw_label == "section_header":
                continue
            text = fragment.text.strip()
            if len(text) < 24 or len(text.split()) < 4:
                continue
            options = []
            for body in bodies:
                if self.overlap(fragment.bbox, body.bbox) < .72:
                    continue
                gap = max(0, fragment.bbox[1] - body.bbox[3], body.bbox[1] - fragment.bbox[3])
                if gap > 28:
                    continue
                # High precision: a candidate must continue a sentence OR be
                # enclosed by two body segments with the same owner.
                neighbours = [r for r in bodies if r.article_id == body.article_id
                                and r is not body and self.overlap(fragment.bbox,r.bbox) >= .72]
                before = any(r.bbox[3] <= fragment.bbox[1] and
                                fragment.bbox[1]-r.bbox[3] <= 35 for r in [body,*neighbours])
                after = any(r.bbox[1] >= fragment.bbox[3] and
                            r.bbox[1]-fragment.bbox[3] <= 35 for r in [body,*neighbours])
                continuation = (before and after) or (before and text[0].islower())
                if continuation:
                    options.append(body.article_id)
            if not options:
                options = self.interpolated_column_owner(fragment, bodies)
            if len(set(options)) != 1:
                continue
            owner = options[0]
            fragment.type = RegionType.ARTICLE_BODY
            fragment.article_id = owner
            fragment.metadata["article_candidate_id"] = owner
            fragment.metadata["content_scope"] = "main"
            fragment.metadata["include_in_main_body"] = True
            fragment.metadata["editorial_fragment_refinement"] = {
                "status":"assigned", "method":"enclosed_sentence_fragment_v1"}
            report.append({"page":page.pdf_page, "article_id":owner,
                            "region_id":fragment.metadata.get("region_id"), "chars":len(text)})
        return report

    def enrich_document(self, pages: list[PageRecord]) -> list[dict]:
        return [entry for page in pages for entry in self.enrich_page(page)]