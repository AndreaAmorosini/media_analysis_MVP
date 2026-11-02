"""CLI del Knowledge Graph."""

from __future__ import annotations

import argparse
import hashlib
import json

from tqdm import tqdm
from openai import OpenAI


from .articles import read_article, iter_articles
from .resilient_extraction import (Extractor, VERSION, PROMPT)

from .ontology import VERSION as ONTOLOGY_VERSION
from .entity_ontology import VERSION as ENTITY_ONTOLOGY_VERSION
from .graph_store import GraphStore
from .settings import CFG

from .predicate_catalog import (
    VERSION as NEWS_PREDICATE_VERSION,
)

from .extraction_rules import (
    VERSION as EXTRACTION_RULES_VERSION,
)


def signature() -> str:
    cfg = {
        "version": VERSION,
        "model": CFG.model_id,
        "window_chars": CFG.window_chars,
        "window_overlap": CFG.window_overlap,
        "max_output_tokens": CFG.max_output_tokens,
        "json_mode": CFG.json_mode,
        "prompt_sha256": hashlib.sha256(PROMPT.encode("utf-8")).hexdigest(),
        "ontology_version": ONTOLOGY_VERSION,
        "entity_ontology_version": ENTITY_ONTOLOGY_VERSION,
        "news_predicate_ontology": NEWS_PREDICATE_VERSION,
        "semantic_rules_version": EXTRACTION_RULES_VERSION,
        "prompt_sha256": hashlib.sha256(PROMPT.encode("utf-8")).hexdigest(),
    }

    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()


def model_check():
    client = OpenAI(
        base_url=CFG.base_url,
        api_key=CFG.api_key,
        timeout=30,
    )

    models = [model.id for model in client.models.list().data]

    print("Modelli serviti da Unsloth:", models)

    # if CFG.model_id not in models:
    #     raise ValueError(f"UNSLOTH_MODEL={CFG.model_id!r} non presente nell'endpoint")


def run_preflight():
    model_check()

    store = GraphStore()

    try:
        store.verify()
        print("Neo4j connesso:", CFG.neo4j_uri)
    finally:
        store.close()

    print("Directory input:", CFG.data_dir)
    print("Finestra:", CFG.window_chars, "caratteri")
    print("Max output tokens:", CFG.max_output_tokens)


def run_ingest(limit: int | None, replace: bool, no_cache: bool, missing_only: bool):
    store = GraphStore()

    try:
        store.verify()
        store.init()

        extractor = Extractor(use_window_cache=not no_cache)
        sig = signature()

        CFG.output_dir.mkdir(parents=True, exist_ok=True)

        ok = 0
        skipped = 0
        failed = 0
        scanned = 0
        
        paths = sorted(CFG.data_dir.rglob("*.json"))
        total = limit if (limit is not None or limit != 0) else len(paths)
        # pbar = tqdm(total=total, desc="Press KG", unit="articolo")


        for path in tqdm(paths, desc="Press KG", unit="articolo"):
            try:
                article = read_article(path,CFG.data_dir)

                if article is None:
                    continue
                if (limit is not None and scanned >= limit):
                    break


                status = store.status(article.id)
                
                if missing_only and status is not None:
                    skipped += 1
                    continue
                
                if limit is not None and scanned >= limit:
                    break
                
                scanned += 1
                # pbar.update(1)

                if (status == (article.content_hash, sig)  and not replace):
                    skipped += 1
                    continue
                
                if (status is not None and not replace):
                    raise ValueError("Articolo cambiato o modello/config diversi: usare --replace o un nuovo database")

                cache = (CFG.output_dir / f"{article.id}.json")

                payload = None

                if cache.is_file() and not no_cache:
                    previous = json.loads(cache.read_text(encoding="utf-8"))

                    if (previous.get("content_hash") == article.content_hash and previous.get("signature") == sig):
                        payload = previous["payload"]

                if payload is None:
                    payload = extractor.extract(article)
                    temp = cache.with_suffix(".tmp")
                    temp.write_text(
                        json.dumps(
                            {
                                "content_hash": article.content_hash,
                                "signature": sig,
                                "payload": payload,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )

                    temp.replace(cache)

                result = store.write(article, payload, sig, replace=replace,)

                if result == "written":
                    ok += 1
                elif result == "skipped":
                    skipped += 1

            except Exception as exc:
                failed += 1
                tqdm.write(f"ERROR {path}: {exc}")
                
        # pbar.close()
        print(
            f"Scritti: {ok}; "
            f"saltati: {skipped}; "
            f"falliti: {failed}; "
            f"esaminati: {scanned}"
        )

        if failed:
            raise SystemExit(1)

    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser()

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight")
    sub.add_parser("init-db")

    ingest = sub.add_parser("ingest")

    ingest.add_argument("--limit", type=int, default=None)
    ingest.add_argument("--replace", action="store_true")
    ingest.add_argument("--no-cache", action="store_true")
    ingest.add_argument("--missing-only",action="store_true", help="Elabora soltanto articoli assenti in Neo4j")

    sub.add_parser("stats")

    args = parser.parse_args()

    if args.command == "preflight":
        run_preflight()

    elif args.command == "ingest":
        if args.missing_only and args.replace:
            parser.error("--missing-only e --replace non disponibili contemporaneamente:")
        run_ingest(
            args.limit,
            args.replace,
            args.no_cache,
            args.missing_only,
        )

    else:
        store = GraphStore()
        try:
            if args.command == "init-db":
                store.init()
                print("Constraints inizializzati")
            else:
                print(json.dumps(store.stats(), ensure_ascii=False, indent=2))

        finally:
            store.close()


if __name__ == "__main__":
    main()