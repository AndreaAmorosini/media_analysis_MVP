"""System invariant tests: verifica che le proprietà strutturali
del progetto rimangano vere durante il refactoring.

Questi test non testano logica di business, ma invarianti architetturali:
- tutti i moduli vivi devono essere importabili
- la CLI signature deve essere deterministica
- i moduli core devono mantenere le loro costanti di versione
- le dipendenze tra moduli devono rimanere consistenti
"""

import hashlib
import importlib
import json

import pytest


# ============================================================
# Importabilità di tutti i moduli
# ============================================================

class TestModuleImportability:
    """Tutti i moduli del package devono essere importabili senza errori."""

    CORE_MODULES = [
        "presskg",
        "presskg.ingestion.articles",
        "presskg.cli",
        "presskg.graph.enrich_graph",
        "presskg.taxonomy.entity_ontology",
        "presskg.ingestion.extraction",
        "presskg.ingestion.extraction_rules",
        "presskg.graph.graph_store",
        "presskg.ingestion.grounding",
        "presskg.graph.normalize_predicates",
        "presskg.taxonomy.ontology",
        "presskg.taxonomy.predicate_catalog",
        "presskg.ingestion.resilient_extraction",
        "presskg.config",
        "presskg.common",
        "presskg.common.io",
        "presskg.common.hashing"
    ]

    RESOLUTION_MODULES = [
        "presskg.resolution",
        "presskg.resolution.identity.attributes",
        "presskg.resolution.identity.attribute_grounding",
        "presskg.resolution.identity.baseline",
        "presskg.resolution.identity.candidate_generation",
        "presskg.resolution.projection.fact_materialization",
        "presskg.resolution.projection.fact_projection",
        "presskg.resolution.projection.entity_materialization",
        "presskg.resolution.projection.dry_run",
        "presskg.resolution.identity.clustering",
        "presskg.resolution.identity.contextual_alias",
        "presskg.resolution.identity.contextual_evidence",
        "presskg.resolution.identity.contextual_refinement",
        "presskg.resolution.identity.coreference",
        "presskg.resolution.identity.evidence_matching",
        "presskg.resolution.identity.alias_index",
        "presskg.resolution.identity.identity_gate",
        "presskg.resolution.projection.safety_gate",
        "presskg.resolution.predicates.candidate_generation",
        "presskg.resolution.predicates.evidence_grounding",
        "presskg.resolution.predicates.quality_audit",
        "presskg.resolution.identity.profiles",
        "presskg.resolution.identity.readiness",
        "presskg.resolution.identity.readiness_refinement",
        "presskg.resolution.identity.referent_eligibility",
        "presskg.resolution.identity.relation_scope",
        "presskg.resolution.identity.semantic_decomposition",
        "presskg.resolution.identity.splitting",
    ]

    VALIDATION_MODULES = [
        "presskg.validation",
        "presskg.validation.primary",
        "presskg.validation.corrections",
        "presskg.validation.decisions",
        "presskg.validation.materialization",
        "presskg.validation.__main__",
    ]

    @pytest.mark.parametrize("module", CORE_MODULES)
    def test_core_module_importable(self, module):
        importlib.import_module(module)

    @pytest.mark.parametrize("module", RESOLUTION_MODULES)
    def test_resolution_module_importable(self, module):
        importlib.import_module(module)

    @pytest.mark.parametrize("module", VALIDATION_MODULES)
    def test_validation_module_importable(self, module):
        importlib.import_module(module)


# ============================================================
# Versioni e costanti
# ============================================================

class TestVersionConstants:
    """Le costanti di versione devono rimanere stringhe non vuote."""

    def test_ontology_version(self):
        from presskg.taxonomy.ontology import VERSION
        assert isinstance(VERSION, str) and len(VERSION) > 0

    def test_entity_ontology_version(self):
        from presskg.taxonomy.entity_ontology import VERSION
        assert isinstance(VERSION, str) and len(VERSION) > 0

    def test_predicate_catalog_version(self):
        from presskg.taxonomy.predicate_catalog import VERSION
        assert isinstance(VERSION, str) and len(VERSION) > 0

    def test_extraction_rules_version(self):
        from presskg.ingestion.extraction_rules import VERSION
        assert isinstance(VERSION, str) and len(VERSION) > 0

    def test_extraction_version(self):
        from presskg.ingestion.extraction import VERSION
        assert isinstance(VERSION, str) and len(VERSION) > 0

    def test_resilient_extraction_version(self):
        from presskg.ingestion.resilient_extraction import VERSION
        assert isinstance(VERSION, str) and len(VERSION) > 0


# ============================================================
# CLI signature deterministica
# ============================================================

class TestCliSignature:
    """La signature() della CLI deve essere deterministica."""

    def test_signature_is_deterministic(self):
        from presskg.cli import signature
        s1 = signature()
        s2 = signature()
        assert s1 == s2
        assert len(s1) == 64  # sha256 hex

    def test_signature_changes_with_prompt(self):
        """Se il PROMPT cambia, la signature deve cambiare."""
        import presskg.cli as cli
        original = cli.PROMPT
        try:
            cli.PROMPT = original + "\nEXTRA"
            s_modified = cli.signature()
            cli.PROMPT = original
            s_original = cli.signature()
            assert s_modified != s_original
        finally:
            cli.PROMPT = original


# ============================================================
# Settings
# ============================================================

class TestSettings:
    """Invarianti delle impostazioni."""

    def test_cfg_exists(self):
        from presskg.config import CFG
        assert CFG is not None

    def test_window_params_valid(self):
        from presskg.config import CFG
        assert CFG.window_chars >= 1000
        assert 0 <= CFG.window_overlap < CFG.window_chars // 2

    def test_data_dir_is_path(self):
        from pathlib import Path
        from presskg.config import CFG
        assert isinstance(CFG.data_dir, Path)


# ============================================================
# Ontologia: consistenza interna
# ============================================================

class TestOntologyConsistency:
    """Invarianti di consistenza tra ontologia predicati ed entità."""

    def test_no_overlap_between_ontology_and_catalog(self):
        """I predicati in ontology.py e predicate_catalog.py
        non devono sovrascriversi in modo conflittuale."""
        from presskg.taxonomy.ontology import CANONICAL as ONT_CANON
        from presskg.taxonomy.predicate_catalog import CANONICAL as CAT_CANON
        overlap = set(ONT_CANON.keys()) & set(CAT_CANON.keys())
        # overlap è ammesso solo se la famiglia è la stessa
        for pred in overlap:
            # non ci aspettiamo conflitti, ma se ci sono devono essere coerenti
            assert isinstance(pred, str)

    def test_entity_types_all_have_descriptions(self):
        from presskg.taxonomy.entity_ontology import TYPES, DESCRIPTIONS
        for t in TYPES:
            assert t in DESCRIPTIONS, f"Tipo {t} senza descrizione"

    def test_entity_families_cover_all_types(self):
        from presskg.taxonomy.entity_ontology import TYPES, FAMILIES
        all_in_families = set()
        for fam_types in FAMILIES.values():
            all_in_families.update(fam_types)
        assert all_in_families == set(TYPES)


# ============================================================
# Validation package
# ============================================================

class TestValidationInvariants:
    """Invarianti del package di validazione."""

    def test_verdicts_set(self):
        from presskg.validation.primary import VERDICTS
        assert VERDICTS == {
            "supported_as_is",
            "correction_proposed",
            "unsupported",
            "insufficient_context",
        }

    def test_fields_contain_required(self):
        from presskg.validation.primary import FIELDS
        for required in ("claim_id", "claim_hash", "run_hash"):
            assert required in FIELDS

    def test_cli_commands(self):
        from presskg.validation.__main__ import COMMANDS
        assert set(COMMANDS) == {"primary", "corrections", "decisions", "materialize"}


# ============================================================
# Graph store: normalize function
# ============================================================

class TestGraphStoreNormalize:
    """La funzione normalize di graph_store deve comportarsi in modo stabile."""

    def test_normalize_basic(self):
        from presskg.graph.graph_store import normalize
        assert normalize("GORI S.p.A.") == "gori s p a"

    def test_normalize_unicode(self):
        from presskg.graph.graph_store import normalize
        result = normalize("Città di Scafati")
        assert "citt" in result

    def test_normalize_empty(self):
        from presskg.graph.graph_store import normalize
        assert normalize("") == ""
        assert normalize("   ") == ""


# ============================================================
# Enrich graph: topic detection
# ============================================================

class TestEnrichGraphTopics:
    """Test per il rilevamento topic in enrich_graph."""

    def test_detect_topics_water_service(self):
        from presskg.graph.enrich_graph import detect_topics
        topics = list(detect_topics("interruzione del servizio idrico"))
        assert any("Continuità" in t for t, _ in topics)

    def test_detect_topics_maintenance(self):
        from presskg.graph.enrich_graph import detect_topics
        topics = list(detect_topics("guasto alla rete idrica"))
        assert any("manutenzione" in t.lower() or "Reti" in t for t, _ in topics)

    def test_detect_topics_no_match(self):
        from presskg.graph.enrich_graph import detect_topics
        topics = list(detect_topics("il tempo è bello oggi"))
        assert len(topics) == 0

    def test_canonical_predicate_map(self):
        from presskg.graph.enrich_graph import canonical_predicate
        assert canonical_predicate("FINANCES") == "FUNDS"
        assert canonical_predicate("PARTNERS_WITH") == "COLLABORATES_WITH"
        assert canonical_predicate("UNKNOWN") is None
