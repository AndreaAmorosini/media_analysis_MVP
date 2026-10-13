"""Bridge a web title-only page to narrative columns on following PDF pages.

The first continuation page must carry a repeated title at the beginning of
its main text lane. Subsequent pages inherit ONLY that verified lane.
Keeps sidebar bodies unassigned. Creates FlowLinks for provenance and reading
order. Does not require the continuation page to be classified as WEB.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import unquote, urlsplit
from press_reputation.models.page import PageRecord, PageType, SourceType, RegionType
from press_reputation.reconstruction.flow_models import FlowLink


class AnchoredWebContinuationResolver:
    BLOCKED = {"related", "advertisement", "boilerplate", "navigation", "non_main"}
    MAX_NEXT_PAGES = 5

    @staticmethod
    def words(text):
        return re.findall(r"[a-zà-ÿ0-9]+", (text or "").casefold())

    @classmethod
    def similarity(cls, a, b):
        ta,tb=cls.words(a),cls.words(b)
        if len(ta)<4 or len(tb)<4:
            return 0.
        recall=len(set(ta)&set(tb))/max(1,len(set(ta)))
        return .65*recall+.35*SequenceMatcher(None,' '.join(ta),' '.join(tb)).ratio()

    @staticmethod
    def lane_x(region,page):
        return region.bbox[0]/(page.page_width or 595)

    def main_anchor(self,page):
        titles=[r for r in page.regions if r.type==RegionType.ARTICLE_TITLE
                and r.article_id and r.text and not r.exclude_from_article_text
                and r.metadata.get('title_role') not in {'teaser','not_main','ambiguous'}]
        if not page.source.url:
            return None
        slug=unquote(urlsplit(page.source.url).path.strip('/').split('/')[-1]).replace('-',' ').replace('_',' ')
        ranked=sorted([(self.similarity(t.text,slug),t) for t in titles],key=lambda s:s[0],reverse=True)
        if not ranked or ranked[0][0] < .70:
            return None
        if len(ranked)>1 and ranked[0][0]-ranked[1][0]<.15:
            return None
        return ranked[0][1]

    def lane_regions(self, page, lane, *, top_page=False):
        body=[r for r in page.regions if r.type==RegionType.ARTICLE_BODY and r.text and
                r.bbox and not r.exclude_from_article_text and
                r.metadata.get('content_scope') not in self.BLOCKED and
                not r.article_id]
        # Full-column text only; very short fragments are left for usual resolvers.
        candidates=[r for r in body if len(r.text)>100 and abs(self.lane_x(r,page)-lane)<.065]
        return candidates

    def find_first_lane(self,page,title):
        # An exact repeated article title (even if UNKNOWN) is a strong zone prior.
        candidates=[]
        for r in page.regions:
            if (not r.text or not r.bbox or r.exclude_from_article_text or
                r.metadata.get('content_scope') in self.BLOCKED):
                continue
            score=self.similarity(title,r.text)
            if score>=.84 and len(r.text)<180:
                candidates.append((score,r))
        if len(candidates)!=1:
            return None
        region=candidates[0][1]
        lane=self.lane_x(region,page)
        main=self.lane_regions(page,lane,top_page=True)
        # Require substantial narrative text after the title in the SAME lane.
        if sum(len(r.text) for r in main if r.bbox[1]>=region.bbox[1]) < 250:
            return None
        return lane

    def enrich_document(self, pages: list[PageRecord]) -> list[FlowLink]:
        links=[]
        ordered=sorted(pages,key=lambda p:(p.document_id,p.pdf_page))
        by_num={(p.document_id,p.pdf_page):p for p in ordered}
        for start in ordered:
            anchor=self.main_anchor(start)
            if anchor is None:
                continue
            # Anchor must have no own main body, otherwise the other linkers
            # should handle the continuation from existing material.
            if any(r.type==RegionType.ARTICLE_BODY and r.article_id==anchor.article_id
                    for r in start.regions):
                continue
            first=by_num.get((start.document_id,start.pdf_page+1))
            if not first:
                continue
            lane=self.find_first_lane(first,anchor.text)
            if lane is None:
                continue
            prev=start
            for offset in range(1,self.MAX_NEXT_PAGES+1):
                page=by_num.get((start.document_id,start.pdf_page+offset))
                if not page:
                    break
                if (start.source.publication_date and page.source.publication_date
                    and start.source.publication_date!=page.source.publication_date):
                    break
                if page.source.url and page.source.url!=start.source.url:
                    break
                # Other *confirmed* titles are a hard new-article barrier.
                if any(r.type==RegionType.ARTICLE_TITLE and r.article_id and
                        r.article_id!=anchor.article_id and not r.exclude_from_article_text
                        and r.metadata.get('title_role') not in {'teaser','not_main'}
                        for r in page.regions):
                    break
                regions=self.lane_regions(page,lane)
                if not regions:
                    break
                # Never override an existing candidate from another article.
                regions=[r for r in regions if r.metadata.get('article_candidate_id') in (None,anchor.article_id)]
                if sum(len(r.text) for r in regions)<120:
                    break
                for r in regions:
                    r.article_id=anchor.article_id
                    r.metadata['article_candidate_id']=anchor.article_id
                    r.metadata['content_scope']='main'
                    r.metadata['include_in_main_body']=True
                    r.metadata['anchored_web_continuation']={
                        'method':'repeated_title_verified_lane_v1',
                        'from_pdf_page':prev.pdf_page,'anchor_pdf_page':start.pdf_page,
                        'lane_relative_x':round(lane,3)}
                # The profiler can call a browser-print continuation CLIPPING
                # because of repeated DataStampa chrome. Once the repeated
                # title/lane is verified, correct page-level editorial metadata.
                page.page_type = PageType.WEB
                page.source.type = SourceType.WEB
                if not page.source.name:
                    page.source.name = start.source.name
                if not page.source.url:
                    page.source.url = start.source.url
                links.append(FlowLink(
                    document_id=start.document_id, article_candidate_id=anchor.article_id,
                    from_pdf_page=prev.pdf_page, to_pdf_page=page.pdf_page,
                    method='repeated_title_verified_lane_v1',status='accepted',
                    confidence=.90 if offset==1 else .82,
                    evidence=['repeated_title_in_first_body_lane','consistent_column_lane',
                                'compatible_publication_date'],
                    from_region_id=anchor.metadata.get('region_id') if offset==1 else None,
                    to_region_id=regions[0].metadata.get('region_id'),
                ))
                prev=page
        return links