
"""PressKG Phase 4F.1: Projection Readiness Refinement.

Generic, corpus-derived corrections to Phase 4F:
- validate personal names containing surname particles
- recover well-supported Project identities
- recover qualified MediaOutlet identities
- detect OCR-concatenated multi-entity surfaces
- preserve all other upstream decisions

Read-only. No LLM calls. No Neo4j writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata

from collections import Counter, defaultdict
from pathlib import Path


VERSION = "projection-readiness-refinement-v1"

APPROVED = "APPROVED_FOR_PROJECTION"
CONTEXTUAL = "CONTEXTUAL_ONLY"
BLOCKED = "BLOCKED"

STATUSES = {APPROVED, CONTEXTUAL, BLOCKED}

PARTICLES = {
    "di", "de", "del", "della", "da",
    "van", "von",
}

ARTICLES = {
    "il", "lo", "la", "l", "i", "gli", "le",
}

GENERIC_PREDICATES = {
    "OTHER", "MENTIONS",
    "RELATED_TO", "ASSOCIATED_WITH",
}

INDEFINITE = {
    "un", "uno", "una",
    "due", "tre",
    "alcuni", "alcune",
    "molti", "molte",
}

PROJECT_KINDS = {"Project"}
MEDIA_KINDS = {"MediaOutlet"}

# Nomi di ruoli e cariche, non identità del corpus.
# La lista può essere estesa senza introdurre nomi propri.
ROLE_HEADS = {
    "sindaco", "vicesindaco",
    "assessore", "consigliere",
    "presidente", "vicepresidente",
    "ministro", "viceministro",
    "sottosegretario", "prefetto",
    "questore", "commissario",
    "direttore", "responsabile",
    "amministratore", "segretario",
    "governatore",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(
            lambda: f.read(1 << 20), b""
        ):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(
        Path(path).read_text(encoding="utf-8")
    )


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{n}: {exc}"
                ) from exc


def write_jsonl(path, records):
    with Path(path).open(
        "w", encoding="utf-8"
    ) as f:
        for row in records:
            f.write(
                json.dumps(
                    row, ensure_ascii=False, sort_keys=True
                ) + "\n"
            )


def normalize(value):
    value = unicodedata.normalize(
        "NFKD", str(value or "")
    ).casefold()

    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )

    return " ".join(
        re.sub(r"[^\w]+", " ", value).split()
    )


def raw_words(value):
    return re.findall(
        r"[^\W\d_]+",
        str(value or ""),
        flags=re.UNICODE,
    )


def relation_signature(profile):
    ctx = profile.get("fact_context") or {}

    predicate = str(
        ctx.get("predicate") or ""
    ).upper().strip()

    counterpart = normalize(
        ctx.get("counterpart_name")
    )

    role = str(
        profile.get("role") or ""
    ).lower()

    if (
        not predicate
        or predicate in GENERIC_PREDICATES
        or len(counterpart) < 3
        or role not in {"subject", "object"}
    ):
        return None

    return role, predicate, counterpart


# ============================================================
# Source validation
# ============================================================

def load_inputs(profiles_dir, gate_dir, readiness_dir):
    p = Path(profiles_dir)
    g = Path(gate_dir)
    r = Path(readiness_dir)

    ps = read_json(p / "summary.json")
    gs = read_json(g / "summary.json")
    rs = read_json(r / "summary.json")

    require(
        ps["version"] == "identity-profiles-v1",
        "Profile version mismatch",
    )
    require(
        gs["version"] == "identity-integrity-gate-v1",
        "Gate version mismatch",
    )
    require(
        rs["version"] == "projection-readiness-v1",
        "Readiness version mismatch",
    )

    batch = ps["source_batch_key"]

    require(
        gs["source_batch_key"] == batch
        and rs["source_batch_key"] == batch,
        "Source batch mismatch",
    )

    require(
        rs["source_gate_sha256"]
        == gs["checksums"]["projection_manifest.jsonl"],
        "4E/4F lineage mismatch",
    )

    for summary in (ps, gs, rs):
        require(
            summary["graph_writes"] == 0
            and summary["identity_merges"] == 0,
            "Upstream graph mutation detected",
        )

    files = [
        (
            p / "mention_profiles.jsonl",
            ps["mention_profiles_sha256"],
        ),
        (
            p / "component_profiles.jsonl",
            ps["component_profiles_sha256"],
        ),
    ]

    files += [
        (
            g / filename, expected
        )
        for filename, expected
        in gs["checksums"].items()
    ]

    files += [
        (
            r / filename, expected
        )
        for filename, expected
        in rs["checksums"].items()
    ]

    for path, expected in files:
        require(
            sha256(path) == expected,
            f"Checksum mismatch: {path}",
        )

    profiles = {}

    for filename in (
        "mention_profiles.jsonl",
        "component_profiles.jsonl",
    ):
        for profile in read_jsonl(p / filename):
            pid = profile["profile_id"]

            require(
                pid not in profiles
                and profile["canonical_id"] is None
                and not profile["merge_approved"],
                f"Invalid profile: {pid}",
            )

            profiles[pid] = profile

    def index(path, field):
        result = {}
        for row in read_jsonl(path):
            key = row[field]
            require(
                key not in result,
                f"Duplicate {field}: {key}",
            )
            result[key] = row
        return result

    gate = index(
        g / "assignment_gate.jsonl",
        "profile_id",
    )
    assignments = index(
        r / "readiness_assignments.jsonl",
        "profile_id",
    )
    manifest = index(
        r / "readiness_manifest.jsonl",
        "proposed_identity_id",
    )

    require(
        set(profiles) == set(gate) == set(assignments),
        "Incomplete profile coverage",
    )
    require(
        len(profiles) == rs["profiles_total"]
        and len(manifest) == rs["identities_total"],
        "Inventory count mismatch",
    )

    by_identity = defaultdict(list)

    for pid, row in assignments.items():
        require(
            row["readiness_status"] in STATUSES
            and row["canonical_entity_id"] is None
            and not row["graph_write_approved"],
            f"Invalid readiness assignment: {pid}",
        )

        require(
            row["upstream_gate_status"]
            == gate[pid]["gate_status"],
            f"Upstream status mismatch: {pid}",
        )

        iid = row["proposed_identity_id"]

        require(
            iid == gate[pid]["proposed_identity_id"],
            f"Identity lineage mismatch: {pid}",
        )

        if iid:
            require(
                iid in manifest,
                f"Missing identity in manifest: {iid}",
            )
            by_identity[iid].append(pid)

    for iid, record in manifest.items():
        pids = by_identity[iid]

        approved = {
            pid for pid in pids
            if assignments[pid]["readiness_status"] == APPROVED
        }

        require(
            len(pids) == record["total_occurrences"],
            f"Manifest occurrence mismatch: {iid}",
        )

        require(
            approved == set(record["approved_profile_ids"]),
            f"Manifest approved IDs mismatch: {iid}",
        )

    return (
        ps, gs, rs, profiles,
        gate, assignments, manifest, by_identity,
    )


# ============================================================
# Corpus-derived person-name evidence
# ============================================================

def person_first_name_inventory(
    profiles, assignments
):
    """Learn first-name candidates from independently accepted names.

    Require multiple different full-name surfaces across articles.
    Descriptive phrases with lowercase middle prepositions are
    not used as seed anchors.
    """

    examples = defaultdict(
        lambda: {
            "surnames": set(),
            "articles": set(),
        }
    )

    for pid, profile in profiles.items():
        row = assignments[pid]

        if (
            row["readiness_status"] != APPROVED
            or profile["source_kind"] != "Person"
            or profile["profile_scope"] != "mention_occurrence"
        ):
            continue

        words = raw_words(profile["surface"])

        if len(words) != 2:
            continue

        if not all(
            word[0].isupper() for word in words
        ):
            continue

        first, surname = words

        if normalize(first) in INDEFINITE:
            continue

        item = examples[normalize(first)]
        item["surnames"].add(normalize(surname))
        item["articles"].add(profile["article_id"])

    return {
        first
        for first, evidence in examples.items()
        if (
            len(evidence["surnames"]) >= 2
            and len(evidence["articles"]) >= 2
        )
    }


def particle_person_name(surface, first_names):
    """Validate a person name with a surname particle.

    Requires corpus evidence for the first name and
    excludes occupational/title constructions.

    This is an identity-candidate check, not a full NER system.
    """

    words = raw_words(surface)

    if len(words) < 3:
        return False

    first = words[0]
    last = words[-1]
    middle = words[1:-1]

    first_key = normalize(first)

    if first_key in ROLE_HEADS:
        return False

    if first_key not in first_names:
        return False

    if not (
        first[0].isupper()
        and last[0].isupper()
    ):
        return False

    if not all(
        normalize(word) in PARTICLES
        for word in middle
    ):
        return False

    return True

def recover_person(
    pids, profiles, gate, first_names,
    min_articles=2,
):
    if not pids:
        return False

    source = [profiles[pid] for pid in pids]

    if not all(
        profile["source_kind"] == "Person"
        for profile in source
    ):
        return False

    surfaces = {
        profile["surface"]
        for profile in source
    }

    if not surfaces or not all(
        particle_person_name(name, first_names)
        for name in surfaces
    ):
        return False

    articles = {
        profile["article_id"]
        for profile in source
    }

    if len(articles) < min_articles:
        return False

    # The original gate must have accepted the occurrence
    # before the stricter 4F name-structure filter rejected it.
    return any(
        gate[pid]["gate_status"] == APPROVED
        for pid in pids
    )


# ============================================================
# Project evidence
# ============================================================

def recover_project(
    pids, profiles, gate,
    min_articles=3,
):
    source = [profiles[pid] for pid in pids]

    if not source:
        return False

    approved_sources = [
        profile
        for profile in source
        if gate[profile["profile_id"]]["gate_status"]
        == APPROVED
        and profile["profile_scope"]
        == "mention_occurrence"
    ]

    if not approved_sources:
        return False

    # Event descriptions must not be recovered as Projects.
    if any(
        profile["source_kind"] not in PROJECT_KINDS
        for profile in approved_sources
    ):
        return False

    articles = {
        profile["article_id"]
        for profile in approved_sources
    }

    if len(articles) < min_articles:
        return False

    signatures = {
        relation_signature(profile)
        for profile in approved_sources
    }
    signatures.discard(None)

    # Distinct relations and counterparties support the
    # existence of a stable project referent.
    counterparts = {
        sig[2] for sig in signatures
    }

    return (
        len(signatures) >= 2
        and len(counterparts) >= 2
    )


# ============================================================
# Qualified MediaOutlet recovery
# ============================================================

def recover_media_outlet(
    pids, profiles, gate,
    min_articles=2,
):
    source = [profiles[pid] for pid in pids]

    if not source:
        return False

    if not all(
        profile["source_kind"] in MEDIA_KINDS
        for profile in source
    ):
        return False

    articles = {
        profile["article_id"]
        for profile in source
    }

    if len(articles) < min_articles:
        return False

    surfaces = {
        profile["surface"]
        for profile in source
    }

    if not all(
        len(raw_words(surface)) >= 2
        and raw_words(surface)[0].casefold() in ARTICLES
        and any(
            word[0].isupper()
            for word in raw_words(surface)[1:]
        )
        for surface in surfaces
    ):
        return False

    # Only the original article-stripping warning may be
    # overridden, never other blockers.
    return all(
        gate[pid]["gate_status"] == CONTEXTUAL
        and set(gate[pid]["gate_reasons"])
        == {"article_stripped_unverified_name"}
        and gate[pid]["upstream_status"] == "PROVISIONAL"
        and profile["profile_scope"] == "mention_occurrence"
        for pid, profile in zip(pids, source)
    )


# ============================================================
# Corpus-derived OCR composite detection
# ============================================================

def approved_name_inventory(manifest):
    names = defaultdict(set)

    for iid, row in manifest.items():
        if row["readiness_status"] != APPROVED:
            continue

        for surface in row["surface_counts"]:
            name = normalize(surface)

            if len(name) >= 3:
                names[name.replace(" ", "")].add(iid)

    # Keep only names with unambiguous identities.
    return {
        name: next(iter(ids))
        for name, ids in names.items()
        if len(ids) == 1
    }


def joined_coordination_entities(surface, inventory):
    """Detect A-e-B concatenations with independently known names.

    EiceGori -> Eic + e + Gori.

    Requires a visible capital boundary and independent identity
    anchors for both segments. Does not infer arbitrary words.
    """

    text = str(surface or "")

    # Consider CamelCase boundaries, not arbitrary lowercase
    # substring matches.
    matches = set()

    for match in re.finditer(
        r"([^\W_]+?)(e|ed|con)([A-ZÀ-ÖØ-Þ][^\W_]*)",
        text,
        flags=re.UNICODE,
    ):
        left = normalize(match.group(1)).replace(" ", "")
        right = normalize(match.group(3)).replace(" ", "")

        a = inventory.get(left)
        b = inventory.get(right)

        if a and b and a != b:
            matches.add((a, b))

    return sorted(matches)


def composite_ocr_risk(surface, inventory):
    return bool(
        joined_coordination_entities(surface, inventory)
    )


# ============================================================
# Final decisions
# ============================================================

def evaluate_identity(
    iid,
    manifest_row,
    pids,
    profiles,
    gate,
    first_names,
    inventory,
):
    old = manifest_row["readiness_status"]
    reasons = set(manifest_row["readiness_reasons"])

    surfaces = manifest_row["surface_counts"]

    if old == BLOCKED:
        return BLOCKED, "UNCHANGED_BLOCKED", []

    # Composite detection always takes precedence over recovery.
    composite_evidence = []

    for surface in surfaces:
        pairs = joined_coordination_entities(
            surface, inventory
        )

        for a, b in pairs:
            if a != iid and b != iid:
                composite_evidence.append({
                    "surface": surface,
                    "embedded_identity_ids": [a, b],
                })

    if composite_evidence:
        return (
            CONTEXTUAL,
            "OCR_MULTI_ENTITY_COMPOSITE",
            composite_evidence,
        )

    if old == APPROVED:
        return APPROVED, "UNCHANGED_APPROVED", []

    # Recovery must address exactly the reason for which
    # the identity was downgraded.
    if reasons == {"person_without_full_name_anchor"}:
        if recover_person(
            pids, profiles, gate, first_names
        ):
            return APPROVED, "RECOVER_PARTICLE_PERSON", []

    if reasons == {"descriptive_activity_without_event_anchor"}:
        if recover_project(pids, profiles, gate):
            return APPROVED, "RECOVER_TYPED_PROJECT", []

    if reasons == {"article_stripped_unverified_name"}:
        if recover_media_outlet(pids, profiles, gate):
            return APPROVED, "RECOVER_MEDIA_OUTLET", []

    return CONTEXTUAL, "UNCHANGED_CONTEXTUAL", []


def run(args):
    (
        ps, gs, rs, profiles,
        gate, upstream, manifest, by_identity,
    ) = load_inputs(
        args.profiles_dir,
        args.gate_dir,
        args.readiness_dir,
    )

    first_names = person_first_name_inventory(
        profiles, upstream
    )

    inventory = approved_name_inventory(manifest)

    decisions = {}

    for iid, record in sorted(manifest.items()):
        decisions[iid] = evaluate_identity(
            iid,
            record,
            by_identity[iid],
            profiles,
            gate,
            first_names,
            inventory,
        )

    assignments = []
    approved_by_identity = defaultdict(list)
    occurrence_counts = Counter()
    changes = Counter()

    # A recovery is allowed only for an original, still eligible
    # mention. It never promotes a component or out-of-scope row.
    for pid in sorted(profiles):
        profile = profiles[pid]
        old = upstream[pid]
        original = gate[pid]

        iid = old["proposed_identity_id"]
        old_status = old["readiness_status"]

        status = old_status
        reasons = list(old["readiness_reasons"])
        rule = "UNCHANGED"

        if iid:
            target, identity_rule, evidence = decisions[iid]

            if target == CONTEXTUAL and old_status == APPROVED:
                status = CONTEXTUAL
                reasons = [identity_rule]
                rule = identity_rule

            elif (
                target == APPROVED
                and old_status == CONTEXTUAL
                and identity_rule.startswith("RECOVER_")
            ):
                eligible_original = (
                    profile["profile_scope"]
                    == "mention_occurrence"
                    and original["upstream_status"]
                    == "PROVISIONAL"
                )

                if identity_rule == "RECOVER_MEDIA_OUTLET":
                    allowed = (
                        eligible_original
                        and original["gate_status"] == CONTEXTUAL
                        and set(original["gate_reasons"]) == {
                            "article_stripped_unverified_name"
                        }
                    )
                else:
                    allowed = (
                        eligible_original
                        and original["gate_status"] == APPROVED
                    )

                if allowed:
                    status = APPROVED
                    reasons = [identity_rule]
                    rule = identity_rule

        if status == APPROVED:
            require(
                iid is not None
                and profile["profile_scope"]
                == "mention_occurrence",
                f"Invalid approved occurrence: {pid}",
            )
            approved_by_identity[iid].append(pid)

        occurrence_counts[status] += 1

        if status != old_status:
            changes[rule] += 1

        assignments.append({
            **old,
            "upstream_readiness_status": old_status,
            "readiness_status": status,
            "readiness_reasons": reasons,
            "phase4f1_rule": rule,
            "canonical_entity_id": None,
            "graph_write_approved": False,
            "fact_propagation_allowed": False,
        })

    identities = []
    audit = []
    identity_status_counts = Counter()

    for iid, record in sorted(manifest.items()):
        target, rule, evidence = decisions[iid]
        approved_ids = sorted(
            approved_by_identity[iid]
        )

        # Never publish an approved identity with no approved
        # occurrences.
        final_status = (
            target if target != APPROVED or approved_ids
            else CONTEXTUAL
        )

        identity_status_counts[final_status] += 1

        result = {
            **record,
            "upstream_readiness_status":
                record["readiness_status"],
            "readiness_status": final_status,
            "readiness_reasons": (
                [rule] if rule != "UNCHANGED_CONTEXTUAL"
                and rule != "UNCHANGED_APPROVED"
                and rule != "UNCHANGED_BLOCKED"
                else record["readiness_reasons"]
            ),
            "phase4f1_rule": rule,
            "phase4f1_evidence": evidence,
            "approved_profile_ids": approved_ids,
            "approved_occurrences": len(approved_ids),
            "canonical_entity_created": False,
            "graph_write_approved": False,
        }

        identities.append(result)

        if (
            final_status != record["readiness_status"]
            or rule == "OCR_MULTI_ENTITY_COMPOSITE"
        ):
            audit.append(result)

    require(
        len(assignments) == len(profiles),
        "Incomplete assignment coverage",
    )
    require(
        sum(occurrence_counts.values()) == len(profiles),
        "Status coverage mismatch",
    )
    require(
        sum(
            r["approved_occurrences"] for r in identities
        ) == occurrence_counts[APPROVED],
        "Approved occurrence mismatch",
    )
    require(
        all(
            row["readiness_status"] == APPROVED
            or not row["approved_profile_ids"]
            for row in identities
        ),
        "Non-approved identity contains approved occurrences",
    )

    out = Path(args.out_dir)
    require(
        not out.exists(),
        "Output directory already exists",
    )
    out.mkdir(parents=True)

    assignment_path = out / "refined_assignments.jsonl"
    manifest_path = out / "refined_manifest.jsonl"
    audit_path = out / "refinement_audit.jsonl"

    write_jsonl(assignment_path, assignments)
    write_jsonl(manifest_path, identities)
    write_jsonl(audit_path, audit)

    summary = {
        "version": VERSION,
        "source_batch_key": ps["source_batch_key"],
        "source_readiness_sha256": sha256(
            Path(args.readiness_dir)
            / "readiness_manifest.jsonl"
        ),
        "profiles_total": len(profiles),
        "identities_total": len(identities),
        "corpus_first_name_candidates": len(first_names),
        "name_inventory_size": len(inventory),
        "previous_status_counts":
            rs["readiness_assignment_status_counts"],
        "refined_status_counts":
            dict(sorted(occurrence_counts.items())),
        "identity_status_counts":
            dict(sorted(identity_status_counts.items())),
        "changed_occurrence_counts":
            dict(sorted(changes.items())),
        "identity_rule_counts": dict(sorted(
            Counter(row["phase4f1_rule"] for row in identities).items()
        )),
        "audited_identities": len(audit),
        "graph_writes": 0,
        "identity_merges": 0,
        "canonical_entities_created": 0,
        "llm_calls": 0,
        "checksums": {
            path.name: sha256(path)
            for path in (
                assignment_path,
                manifest_path,
                audit_path,
            )
        },
    }

    (out / "summary.json").write_text(
        json.dumps(
            summary, ensure_ascii=False,
            indent=2, sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(
        summary, ensure_ascii=False, indent=2
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profiles-dir",
        default="output/resolution/identity-profiles-v1",
    )
    parser.add_argument(
        "--gate-dir",
        default="output/resolution/identity-integrity-gate-v1",
    )
    parser.add_argument(
        "--readiness-dir",
        default="output/resolution/projection-readiness-v1",
    )
    parser.add_argument(
        "--out-dir",
        default="output/resolution/projection-readiness-refinement-v1",
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
