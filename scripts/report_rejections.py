#!/usr/bin/env python3
"""Aggregate *_rejections.json diagnostics from PressKG resilient extractor v3.1."""
import argparse
import json
from collections import Counter
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("diagnostics_dir", type=Path)
    args = parser.parse_args()

    if not args.diagnostics_dir.is_dir():
        parser.error(f"Directory non trovata: {args.diagnostics_dir}")

    types = Counter()
    schema = Counter()
    reasons = Counter()
    files = 0

    for path in args.diagnostics_dir.rglob("*_rejections.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        files += 1
        reasons.update(data.get("reasons", {}))
        for issue in data.get("issues", []):
            if issue.get("reason") == "invalid_type":
                for detail in issue.get("details", []):
                    types[(detail.get("role"), str(detail.get("raw_type")))] += 1
            elif issue.get("reason") == "claim_schema":
                for err in issue.get("errors", []):
                    schema[(".".join(str(x) for x in err.get("loc", [])), err.get("type", "unknown"))] += 1

    print(f"File diagnostici: {files}")
    print("\nMotivi di scarto:")
    for key, value in reasons.most_common():
        print(f"  {key}: {value}")
    print("\nTipi di entità non riconosciuti:")
    for (role, label), count in types.most_common(40):
        print(f"  {role:8s} {label:35.35s} {count:5d}")
    print("\nErrori Pydantic per campo:")
    for (field, errtype), count in schema.most_common(40):
        print(f"  {field:35.35s} {errtype:25.25s} {count:5d}")


if __name__ == "__main__":
    main()
