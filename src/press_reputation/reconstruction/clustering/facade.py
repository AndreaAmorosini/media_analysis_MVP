from __future__ import annotations
from press_reputation.core.config import ArticleClusteringConfig
from press_reputation.core.models.page import PageRecord, Region
from press_reputation.reconstruction.clustering.clustering_core import ClusteringCore
from press_reputation.reconstruction.clustering.clustering_columns import ColumnClustering
from press_reputation.reconstruction.clustering.clustering_assignment import AssignmentClustering
from press_reputation.reconstruction.clustering.clustering_flow import FlowClustering

class ArticleClusteringResolver:
    #Intgerfaccia: compone core + columns + assignment + flow
    
    EXCLUDED_TYPES = ClusteringCore.EXCLUDED_TYPES
    MEDIA_TYPES = ClusteringCore.MEDIA_TYPES
    
    def __init__(self, config: ArticleClusteringConfig | None = None) -> None:
        self.core = ClusteringCore(config)
        self.columns = ColumnClustering(self.core)
        self.assignment = AssignmentClustering(self.core, self.columns)
        self.flow = FlowClustering(self.core)
        
    @staticmethod
    def region_id(region: Region) -> str | None:
        return ClusteringCore.region_id(region)

    def eligible(self, region: Region) -> bool:
        return self.core.eligible(region)

    def assign(self, region, article_id, **kwargs) -> bool:
        return self.core.assign(region, article_id, **kwargs)

    @staticmethod
    def source_span(region: Region):
        return ClusteringCore.source_span(region)

    def body_columns(self, page: PageRecord):
        return self.columns.body_columns(page)

    # --- Deleghe: i metodi pubblici usati da pipeline.py ---
    def assign_local(self, pages, matches, entries=None):
        return self.assignment.assign_local(pages, matches, entries)

    def link_accepted_flows(self, pages, links):
        return self.flow.link_accepted_flows(pages, links)

    def detach_rejected_web_pages(self, pages, links):
        return self.flow.detach_rejected_web_pages(pages, links)

    def propagate_web_article_metadata(self, pages, links):
        return self.flow.propagate_web_article_metadata(pages, links)

    def assign_unassigned_body_in_owned_columns(self, page):
        return self.assignment.assign_unassigned_body_in_owned_columns(page)

    def recover_column_continuations(self, page):
        return self.assignment.recover_column_continuations(page)

    def assign_recovered_body(self, page):
        return self.assignment.assign_recovered_body(page)

    def assign_section_headers_and_media(self, page):
        return self.assignment.assign_section_headers_and_media(page)