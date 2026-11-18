"""Characterization tests per i moduli presskg.resolution.

Questi test catturano il振 comportamento attuale dei moduli di resolution
per garantire che il refactoring non cambi la logica di business.
Non richiedono Neo4j: testano solo le funzioni pure di trasformazione dati.
"""

import pytest


# ============================================================
# candidate_generation
# ============================================================

class TestCandidateGeneration:
    """Characterization tests per candidate_generation."""

    def test_normalize(self):
        from presskg.resolution.identity.candidate_generation import normalize
        assert normalize("  GÓRI, S.p.A. ") == "gori s p a"
        assert normalize("Mario Rossi") == "mario rossi"

    def test_acronym(self):
        from presskg.resolution.identity.candidate_generation import acronym
        assert acronym("Ente Idrico Campano") == "eic"
        # una singola parola non genera acronimo (richiede 2-8 token significativi)
        assert acronym("GORI") is None

    def test_legal_base(self):
        from presskg.resolution.identity.candidate_generation import legal_base
        assert legal_base("GORI S.p.A.") == legal_base("Gori")

    def test_significant_tokens(self):
        from presskg.resolution.identity.candidate_generation import significant_tokens
        tokens = significant_tokens("Ministero delle Infrastrutture")
        assert "ministero" in tokens
        assert "infrastrutture" in tokens
        assert "delle" not in tokens

    def test_generate_pairs_legal_form(self, entity_groups):
        from presskg.resolution.identity.candidate_generation import generate_pairs
        pairs, _ = generate_pairs(entity_groups)
        legal_pairs = [p for p in pairs if "legal_form" in p.get("blocking_reasons", [])]
        assert len(legal_pairs) >= 1
        assert all(not p["identity_approved"] for p in legal_pairs)

    def test_generate_pairs_acronym(self, entity_groups):
        from presskg.resolution.identity.candidate_generation import generate_pairs
        pairs, _ = generate_pairs(entity_groups)
        acr_pairs = [p for p in pairs if "acronym" in p.get("blocking_reasons", [])]
        assert len(acr_pairs) >= 1

    def test_generate_pairs_deterministic(self, entity_groups):
        from presskg.resolution.identity.candidate_generation import generate_pairs
        p1, _ = generate_pairs(entity_groups)
        p2, _ = generate_pairs(entity_groups)
        assert p1 == p2

    def test_eligible_statuses(self):
        from presskg.resolution.identity.candidate_generation import eligible
        # PROFILE_ONLY_REASSESS non è in EXCLUDED_STATUSES, ma serve surface non vuota
        assert eligible({"surface": "Sinistra Italiana", "source_kind": "PoliticalParty",
                         "resolution_status": "PROFILE_ONLY_REASSESS"})
        assert not eligible({"surface": "X", "source_kind": "Organization",
                             "resolution_status": "COLLECTIVE_HOLD"})
        assert not eligible({"surface": "attività", "source_kind": "Project",
                             "resolution_status": "PROFILE_ONLY_HELD"})
        # kind esclusi
        assert not eligible({"surface": "100 euro", "source_kind": "Money",
                             "resolution_status": "OK"})

    def test_pair_types_conflict(self):
        from presskg.resolution.identity.candidate_generation import pair_types, normalize
        a = {"normalized_surface": normalize("Marina"),
             "kind_counts": {"Person": 1}}
        b = {"normalized_surface": normalize("Marina SpA"),
             "kind_counts": {"Company": 1}}
        assert pair_types(a, b) == "POTENTIAL_CONFLICT"


# ============================================================
# baseline
# ============================================================

class TestBaseline:
    """Characterization tests per baseline.inspect_fact."""

    def test_valid_fact_no_issues(self, sample_fact):
        from presskg.resolution.identity.baseline import inspect_fact
        assert inspect_fact(sample_fact, "batch-001") == []

    def test_missing_subject(self, sample_fact):
        from presskg.resolution.identity.baseline import inspect_fact
        sample_fact["subjects"] = []
        issues = inspect_fact(sample_fact, "batch-001")
        assert "subject_mention_count_0" in issues

    def test_wrong_batch(self, sample_fact):
        from presskg.resolution.identity.baseline import inspect_fact
        issues = inspect_fact(sample_fact, "wrong-batch")
        assert any("batch" in i for i in issues)

    def test_missing_quote(self, sample_fact):
        from presskg.resolution.identity.baseline import inspect_fact
        sample_fact["original_quote_in_body"] = False
        issues = inspect_fact(sample_fact, "batch-001")
        assert any("quote" in i for i in issues)

    def test_supporting_span_warning_only(self, sample_fact):
        from presskg.resolution.identity.baseline import inspect_fact
        sample_fact["supporting_span_in_body"] = False
        issues = inspect_fact(sample_fact, "batch-001")
        assert issues == ["supporting_span_not_literal"]


# ============================================================
# attributes
# ============================================================

class TestAttributes:
    """Characterization tests per il modulo attributes."""

    def test_module_importable(self):
        from presskg.resolution.identity import attributes
        assert hasattr(attributes, "main") or len(dir(attributes)) > 5

    def test_key_functions_exist(self):
        from presskg.resolution.identity import attributes
        # verifica che le funzioni principali esistano
        funcs = [n for n in dir(attributes) if not n.startswith("_")]
        assert len(funcs) > 3


# ============================================================
# profiles
# ============================================================

class TestProfiles:
    """Characterization tests per il modulo profiles."""

    def test_module_importable(self):
        from presskg.resolution.identity import profiles
        assert profiles is not None

    def test_has_run(self):
        """Dal refactor a pipeline, gli step espongono run(args) -> bool
        invece del vecchio main() con argparse autonomo."""
        from presskg.resolution.identity import profiles
        assert callable(getattr(profiles, "run", None))


# ============================================================
# relation_type_scope
# ============================================================

class TestRelationTypeScope:
    """Characterization tests per relation_type_scope."""

    def test_module_importable(self):
        from presskg.resolution.identity import relation_scope as relation_type_scope
        assert relation_type_scope is not None


# ============================================================
# evidence_matching
# ============================================================

class TestEvidenceMatching:
    """Characterization tests per evidence_matching."""

    def test_module_importable(self):
        from presskg.resolution.identity import evidence_matching
        assert evidence_matching is not None


# ============================================================
# splitting
# ============================================================

class TestSplitting:
    """Characterization tests per splitting."""

    def test_module_importable(self):
        from presskg.resolution.identity import splitting
        assert splitting is not None


# ============================================================
# constrained_clustering
# ============================================================

class TestConstrainedClustering:
    """Characterization tests per constrained_clustering."""

    def test_module_importable(self):
        from presskg.resolution.identity import clustering as constrained_clustering
        assert constrained_clustering is not None


# ============================================================
# projection_readiness
# ============================================================

class TestProjectionReadiness:
    """Characterization tests per projection_readiness."""

    def test_normalize(self):
        from presskg.resolution.identity.readiness import normalize
        result = normalize("  Test  Value  ")
        assert result == "test value"

    def test_tokens(self):
        from presskg.resolution.identity.readiness import tokens
        t = tokens("GORI S.p.A.")
        assert isinstance(t, (set, list, tuple))

    def test_module_importable(self):
        from presskg.resolution.identity import readiness as projection_readiness
        assert projection_readiness is not None


# ============================================================
# semantic_decomposition
# ============================================================

class TestSemanticDecomposition:
    """Characterization tests per semantic_decomposition."""

    def test_require_raises(self):
        from presskg.resolution.identity.semantic_decomposition import require
        with pytest.raises(Exception):
            require(False, "test error")
        require(True, "no error")  # non deve lanciare

    def test_fingerprint_deterministic(self):
        from presskg.resolution.identity.semantic_decomposition import fingerprint
        f1 = fingerprint({"a": 1, "b": 2})
        f2 = fingerprint({"b": 2, "a": 1})
        assert f1 == f2  # stesso contenuto, stesso fingerprint

    def test_read_json(self, tmp_path):
        from presskg.resolution.identity.semantic_decomposition import read_json
        f = tmp_path / "test.json"
        f.write_text('{"key": "value"}', encoding="utf-8")
        assert read_json(f) == {"key": "value"}

    def test_read_jsonl(self, tmp_path):
        # dal P2 la variante locale (con guardia sull'esistenza) si chiama
        # read_jsonl_or_empty; quella comune è read_jsonl.
        from presskg.resolution.identity.semantic_decomposition import read_jsonl_or_empty
        f = tmp_path / "test.jsonl"
        f.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
        rows = list(read_jsonl_or_empty(f))
        assert len(rows) == 2
        assert rows[0] == {"a": 1}
        # file assente: la guardia restituisce lista vuota
        assert list(read_jsonl_or_empty(tmp_path / "missing.jsonl")) == []


# ============================================================
# canonical_fact_materialization
# ============================================================

class TestCanonicalFactMaterialization:
    """Characterization tests per canonical_fact_materialization."""

    def test_require(self):
        from presskg.resolution.projection.fact_materialization import require
        with pytest.raises(Exception):
            require(False, "err")
        require(True, "ok")

    def test_sha256_file(self, tmp_path):
        # dal P2 l'hash di file unificato in presskg.common è sha256_file
        from presskg.resolution.projection.fact_materialization import sha256_file
        f = tmp_path / "f.txt"
        f.write_text("hello", encoding="utf-8")
        h = sha256_file(f)
        assert len(h) == 64

    def test_batches(self):
        from presskg.resolution.projection.fact_materialization import batches
        items = list(range(10))
        result = list(batches(items, 3))
        assert len(result) == 4
        assert result[0] == [0, 1, 2]
        assert result[-1] == [9]


# ============================================================
# canonical_fact_projection
# ============================================================

class TestCanonicalFactProjection:
    """Characterization tests per canonical_fact_projection."""

    def test_module_importable(self):
        from presskg.resolution.projection import fact_projection as canonical_fact_projection
        assert canonical_fact_projection is not None


# ============================================================
# canonical_materialization
# ============================================================

class TestCanonicalMaterialization:
    """Characterization tests per canonical_materialization."""

    def test_module_importable(self):
        from presskg.resolution.projection import entity_materialization as canonical_materialization
        assert canonical_materialization is not None


# ============================================================
# identity_integrity_gate
# ============================================================

class TestIdentityIntegrityGate:
    """Characterization tests per identity_integrity_gate."""

    def test_module_importable(self):
        from presskg.resolution.identity import identity_gate as identity_integrity_gate
        assert identity_integrity_gate is not None


# ============================================================
# materialization_safety_gate
# ============================================================

class TestMaterializationSafetyGate:
    """Characterization tests per materialization_safety_gate."""

    def test_module_importable(self):
        from presskg.resolution.projection import safety_gate as materialization_safety_gate
        assert materialization_safety_gate is not None


# ============================================================
# global_alias_index
# ============================================================

class TestGlobalAliasIndex:
    """Characterization tests per global_alias_index."""

    def test_module_importable(self):
        from presskg.resolution.identity import alias_index as global_alias_index
        assert global_alias_index is not None


# ============================================================
# contextual_evidence / contextual_alias_recovery / contextual_identity_refinement
# ============================================================

class TestContextualModules:
    """Characterization tests per i moduli contextuali."""

    def test_contextual_evidence_importable(self):
        from presskg.resolution.identity import contextual_evidence
        assert contextual_evidence is not None

    def test_contextual_alias_recovery_importable(self):
        from presskg.resolution.identity import contextual_alias as contextual_alias_recovery
        assert contextual_alias_recovery is not None

    def test_contextual_identity_refinement_importable(self):
        from presskg.resolution.identity import contextual_refinement as contextual_identity_refinement
        assert contextual_identity_refinement is not None


# ============================================================
# predicate_* modules
# ============================================================

class TestPredicateModules:
    """Characterization tests per i moduli predicate_*."""

    def test_predicate_candidate_generation_importable(self):
        from presskg.resolution.predicates import candidate_generation as predicate_candidate_generation
        assert predicate_candidate_generation is not None

    def test_predicate_evidence_grounding_importable(self):
        from presskg.resolution.predicates import evidence_grounding as predicate_evidence_grounding
        assert predicate_evidence_grounding is not None

    def test_predicate_quality_audit_importable(self):
        from presskg.resolution.predicates import quality_audit as predicate_quality_audit
        assert predicate_quality_audit is not None


# ============================================================
# referent_eligibility / coreference_audit / projection_readiness_refinement
# ============================================================

class TestRemainingResolutionModules:
    """Characterization tests per i restanti moduli resolution vivi."""

    def test_referent_eligibility_importable(self):
        from presskg.resolution.identity import referent_eligibility
        assert referent_eligibility is not None

    def test_coreference_audit_importable(self):
        from presskg.resolution.identity import coreference as coreference_audit
        assert coreference_audit is not None

    def test_projection_readiness_refinement_importable(self):
        from presskg.resolution.identity import readiness_refinement as projection_readiness_refinement
        assert projection_readiness_refinement is not None

    def test_attribute_grounding_importable(self):
        from presskg.resolution.identity import attribute_grounding
        assert attribute_grounding is not None

    def test_canonical_projection_dry_run_importable(self):
        from presskg.resolution.projection import dry_run as canonical_projection_dry_run
        assert canonical_projection_dry_run is not None
