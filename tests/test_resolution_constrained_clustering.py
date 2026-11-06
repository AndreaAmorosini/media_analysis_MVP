
import unittest

from presskg.resolution.constrained_clustering import (
    build_clusters,
    lexical_identity_key,
)


def group(name, kind="Organization"):
    return {
        "preferred_surface": name,
        "kind_counts": {kind: 10},
        "occurrence_count": 10,
    }


def edge(a, b, n=2, status="COMPATIBLE"):
    return {
        "edge_id": f"{a}-{b}",
        "left_group_id": a,
        "right_group_id": b,
        "attestation_count": n,
        "attestation_ids": [
            f"e{i}{a}{b}" for i in range(n)
        ],
        "type_compatibility": {
            "status": status,
        },
    }


class ConstrainedClusteringTests(unittest.TestCase):

    def test_article_bridge(self):
        groups = {
            "a": group("Ente Idrico Campano"),
            "b": group("EIC"),
            "c": group("l'Ente Idrico Campano"),
        }

        clusters, memberships, accepted, deferred = (
            build_clusters(
                groups,
                {},
                {
                    ("a", "b"): edge("a", "b", 36),
                    ("b", "c"): edge("b", "c", 5),
                },
            )
        )

        self.assertEqual(
            [c["group_count"] for c in clusters],
            [3],
        )
        self.assertEqual(len(deferred), 0)
        self.assertEqual(
            clusters[0]["canonical_surface"],
            "Ente Idrico Campano",
        )

    def test_no_transitive_shortcut(self):
        groups = {
            "a": group("Alpha Alpha"),
            "b": group("AAA"),
            "c": group("Different Name"),
        }

        clusters, memberships, accepted, deferred = (
            build_clusters(
                groups,
                {},
                {
                    ("a", "b"): edge("a", "b", 4),
                    ("b", "c"): edge("b", "c", 2),
                },
            )
        )

        self.assertEqual(len(accepted), 1)
        self.assertEqual(
            deferred[0]["reason"],
            "unsupported_transitive_bridge",
        )

    def test_matching_conflict(self):
        groups = {
            "a": group("Alpha"),
            "b": group("Beta"),
        }

        decisions = {
            ("a", "b"): {
                "verdict": "CONFLICT",
                "hard_conflicts": [
                    "person_vs_organization",
                ],
            },
        }

        clusters, memberships, accepted, deferred = (
            build_clusters(
                groups,
                decisions,
                {
                    ("a", "b"): edge("a", "b"),
                },
            )
        )

        self.assertEqual(len(clusters), 0)
        self.assertEqual(
            deferred[0]["reason"],
            "matching_conflict",
        )

    def test_incompatible_dominant_types(self):
        groups = {
            "a": group("Alpha", "Person"),
            "b": group("Beta", "Company"),
        }

        _, _, _, deferred = build_clusters(
            groups,
            {},
            {("a", "b"): edge("a", "b")},
        )

        self.assertEqual(
            deferred[0]["reason"],
            "unstable_or_incompatible_dominant_types",
        )

    def test_lexical_equivalence_is_not_sufficient(self):
        groups = {
            "a": group("Gori"),
            "b": group("Gori S.p.A."),
        }

        clusters, memberships, accepted, deferred = (
            build_clusters(groups, {}, {})
        )

        self.assertEqual(clusters, [])
        self.assertEqual(accepted, [])

    def test_different_denominations(self):
        self.assertNotEqual(
            lexical_identity_key("Acqua Bene Comune"),
            lexical_identity_key("Acque del Sud"),
        )

    def test_minimum_attestations(self):
        groups = {
            "a": group("Alpha"),
            "b": group("Beta"),
        }

        _, _, _, deferred = build_clusters(
            groups,
            {},
            {("a", "b"): edge("a", "b", 1)},
            min_attestations=2,
        )

        self.assertEqual(
            deferred[0]["reason"],
            "too_few_attestations",
        )


if __name__ == "__main__":
    unittest.main()
