
import unittest

from presskg.resolution.referent_eligibility import (
    component_is_plausible,
    interpret,
    relation_safety,
)


def make_decision(
    name,
    kind,
    classification,
    surfaces=None,
    relation=None,
    quote=None,
):
    components = []
    cursor = 0

    for surface in surfaces or []:
        start = name.index(surface, cursor)
        end = start + len(surface)

        components.append({
            "surface": surface,
            "mention_start": start,
            "mention_end": end,
            "kind": None,
        })

        cursor = end

    evidence = None

    if quote is not None:
        evidence = {
            "field": "original_quote",
            "excerpt": quote,
            "source_fingerprint": "test",
        }

    return {
        "mention_id": "vf-1:subject",
        "fact_id": "vf-1",
        "article_id": "article-1",
        "role": "subject",
        "name": name,
        "kind": kind,
        "classification": classification,
        "rule_id": "test",
        "components": components,
        "relation": relation,
        "evidence": evidence,
    }


class ReferentEligibilityTests(unittest.TestCase):

    def test_named_organizations(self):
        decision = make_decision(
            "Ente Idrico Campano, Regione Campania e Gori",
            "Organization",
            "MULTI_ENTITY",
            [
                "Ente Idrico Campano",
                "Regione Campania",
                "Gori",
            ],
        )

        result, candidates, relations = interpret(
            decision
        )

        self.assertEqual(
            result["interpretation_status"],
            "COMPONENT_CANDIDATES",
        )

        self.assertEqual(
            len(candidates),
            3,
        )

        self.assertTrue(
            all(
                c["eligibility"] == "CANDIDATE"
                for c in candidates
            )
        )

        self.assertEqual(relations, [])

        self.assertEqual(
            result["fact_distribution"],
            "NOT_AUTHORIZED",
        )

    def test_project_activities_not_identities(self):
        decision = make_decision(
            (
                "Lavori per la distrettualizzazione "
                "e il completamento degli schemi fognari"
            ),
            "Project",
            "MULTI_ENTITY",
            [
                "Lavori per la distrettualizzazione",
                "il completamento degli schemi fognari",
            ],
        )

        result, candidates, _ = interpret(
            decision
        )

        self.assertEqual(
            result["interpretation_status"],
            "HOLD_COMPONENTS",
        )

        self.assertTrue(
            all(
                c["eligibility"] == "HELD"
                for c in candidates
            )
        )

    def test_official_name_preserved(self):
        decision = make_decision(
            "Ministero delle Infrastrutture e dei Trasporti",
            "PublicAuthority",
            "COMPOSITE_NAME",
        )

        result, candidates, _ = interpret(
            decision
        )

        self.assertEqual(
            result["interpretation_status"],
            "KEEP_SINGLE",
        )

        self.assertEqual(
            len(candidates),
            1,
        )

        self.assertEqual(
            candidates[0]["identity_surface"],
            decision["name"],
        )

    def test_geographic_hierarchy(self):
        quote = (
            "Il cantiere è attivo "
            "in località Pozzano, a Castellammare."
        )

        decision = make_decision(
            "Pozzano, Castellammare",
            "Location",
            "HIERARCHICAL_REFERENCE",
            ["Pozzano", "Castellammare"],
            relation={
                "subject_index": 0,
                "predicate": "LOCATED_IN",
                "object_index": 1,
                "support_quote": (
                    "in località Pozzano, a Castellammare"
                ),
            },
            quote=quote,
        )

        result, candidates, relations = interpret(
            decision
        )

        self.assertEqual(
            result["interpretation_status"],
            "RELATION_CANDIDATE",
        )

        self.assertEqual(
            len(candidates),
            2,
        )

        self.assertEqual(
            relations[0]["status"],
            "SAFE_FOR_REVIEW",
        )

        self.assertFalse(
            relations[0]["approved"]
        )

    def test_road_scope_is_held(self):
        decision = make_decision(
            (
                "Strada Statale 145, "
                "comune di Castellammare di Stabia"
            ),
            "Location",
            "HIERARCHICAL_REFERENCE",
            [
                "Strada Statale 145",
                "comune di Castellammare di Stabia",
            ],
            relation={
                "subject_index": 0,
                "predicate": "LOCATED_IN",
                "object_index": 1,
                "support_quote": (
                    "Strada Statale 145, nel comune "
                    "di Castellammare di Stabia"
                ),
            },
            quote=(
                "Strada Statale 145, nel comune "
                "di Castellammare di Stabia"
            ),
        )

        result, candidates, relations = interpret(
            decision
        )

        self.assertEqual(
            result["interpretation_status"],
            "HOLD_RELATION",
        )

        self.assertIn(
            "infrastructure_scope_not_verified",
            relations[0]["reasons"],
        )

        self.assertEqual(
            candidates[1]["identity_surface"],
            "Castellammare di Stabia",
        )

    def test_collective_not_split(self):
        decision = make_decision(
            "Le associazioni",
            "Organization",
            "COLLECTIVE",
        )

        result, candidates, _ = interpret(
            decision
        )

        self.assertEqual(
            result["interpretation_status"],
            "COLLECTIVE_HOLD",
        )

        self.assertEqual(candidates, [])

    def test_nonreferential_topic(self):
        decision = make_decision(
            "Riduzione dispersioni e gestione della rete",
            "Topic",
            "NON_REFERENTIAL",
        )

        result, candidates, _ = interpret(
            decision
        )

        self.assertEqual(
            result["interpretation_status"],
            "NON_IDENTITY",
        )

        self.assertEqual(candidates, [])

    def test_unnamed_group_rejected(self):
        eligible, reason = component_is_plausible(
            "pescatori",
            "Organization",
        )

        self.assertFalse(eligible)

        self.assertEqual(
            reason,
            "generic_group_or_activity",
        )

    def test_no_relation_approval(self):
        decision = make_decision(
            "Pozzano, Castellammare",
            "Location",
            "HIERARCHICAL_REFERENCE",
            ["Pozzano", "Castellammare"],
            relation={
                "subject_index": 0,
                "predicate": "LOCATED_IN",
                "object_index": 1,
                "support_quote": "Pozzano, Castellammare",
            },
            quote="Pozzano, Castellammare",
        )

        result, _, relations = interpret(
            decision
        )

        self.assertTrue(
            result["original_fact_preserved"]
        )

        self.assertFalse(
            relations[0]["approved"]
        )

        self.assertEqual(
            result["graph_writes"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
