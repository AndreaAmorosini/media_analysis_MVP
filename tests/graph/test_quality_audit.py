"""Esegue la fase 1 della suite di qualità (audit) sul grafo reale.

Riusa presskg.quality.audit: read-only su Neo4j, scrive solo report in
una directory temporanea.
"""

from pathlib import Path

import pytest

from presskg.config import CFG

pytestmark = pytest.mark.graph


class TestQualityAuditPhase1:

    def test_phase_audit_runs_and_reports(self, quality_store, tmp_path):
        from presskg.quality.audit import get_base, phase_audit

        base = get_base(quality_store)
        report = phase_audit(quality_store, base, tmp_path, Path(CFG.data_dir))

        counts = report["counts"]
        assert counts["articles"] > 0, "Nessun articolo nel grafo: eseguire ingest"
        assert counts["claims"] > 0, "Nessun claim nel grafo"
        assert counts["facts"] > 0, (
            "Nessun arco FACT: eseguire ingest"
        )
        assert counts["global_entities"] > 0
        assert counts["event_mentions"] > 0

        # Report diagnostici materializzati
        for name in (
            "audit.json",
            "offset_errors.csv",
            "orphan_claims.csv",
            "predicate_distribution.csv",
            "entity_type_distribution.csv",
        ):
            assert (tmp_path / name).is_file(), f"report mancante: {name}"

    def test_structural_errors_are_zero(self, quality_store, tmp_path):
        """Zero errori strutturali: offset verificati e nessun claim orfano.

        Coerente con l'audit 7C: se il grafo è integro, gli errori di offset
        e i claim orfani devono essere assenti. Se il corpus locale Data/
        non è disponibile gli offset risultano unverifiable ma non errori.
        """
        from presskg.quality.audit import get_base, phase_audit

        base = get_base(quality_store)
        phase_audit(quality_store, base, tmp_path, Path(CFG.data_dir))

        offset_errors = (tmp_path / "offset_errors.csv").read_text(
            encoding="utf-8"
        ).splitlines()
        orphan_claims = (tmp_path / "orphan_claims.csv").read_text(
            encoding="utf-8"
        ).splitlines()

        assert len(offset_errors) <= 1, (
            "Errori di offset (quote/body_start/body_end non coerenti con il body):\n"
            + "\n".join(offset_errors[1:11])
        )
        assert len(orphan_claims) <= 1, (
            f"Claim orfani (senza ValidatedFact coprente): {len(orphan_claims) - 1}"
        )
