"""CLI del Knowledge Graph."""

from __future__ import annotations

import argparse
import sys
import hashlib
import json

from tqdm import tqdm
from openai import OpenAI


from presskg.ingestion.articles import read_article, iter_articles
from presskg.ingestion.resilient_extraction import (Extractor, VERSION, PROMPT)

from presskg.taxonomy.ontology import VERSION as ONTOLOGY_VERSION
from presskg.taxonomy.entity_ontology import VERSION as ENTITY_ONTOLOGY_VERSION
from presskg.graph.graph_store import GraphStore
from presskg.config import CFG

from presskg.taxonomy.predicate_catalog import (
    VERSION as NEWS_PREDICATE_VERSION,
)

from presskg.ingestion.extraction_rules import (
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

    from presskg.validation.batch import DEFAULT_SECONDARY_JUDGE

    print("Modello di ingestione:", CFG.model_id)
    print("Primo valutatore:", CFG.eval_model_id)
    print(
        "Secondo valutatore:",
        CFG.secondary_eval_model_id or DEFAULT_SECONDARY_JUDGE,
    )

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


def select_article_paths(skip: int = 0, limit: int | None = None) -> list:
    """Selezione deterministica dei file articolo (ordinamento fisso).

    ``skip`` salta i primi N file, ``limit`` e il tetto: con skip=10 e
    limit=20 si ottengono gli articoli 11-30 del dataset, disjoint da
    un run precedente fatto con --limit 10.
    """
    if skip < 0:
        raise ValueError("skip non può essere negativo")
    if limit is not None and limit < 0:
        raise ValueError("limit non può essere negativo")
    paths = sorted(CFG.data_dir.rglob("*.json"))
    end = None if limit is None else skip + limit
    return paths[skip:end]


def run_ingest(limit: int | None, skip: int, replace: bool, no_cache: bool, missing_only: bool):
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
        
        paths = select_article_paths(skip=skip, limit=limit)

        for path in tqdm(paths, desc="Press KG", unit="articolo"):
            try:
                article = read_article(path,CFG.data_dir)

                if article is None:
                    continue

                status = store.status(article.id)
                
                if missing_only and status is not None:
                    skipped += 1
                    continue
                
                scanned += 1

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
    ingest.add_argument(
        "--skip",
        type=int,
        default=0,
        help="Salta i primi N articoli (ordinamento fisso del dataset)",
    )
    ingest.add_argument("--replace", action="store_true")
    ingest.add_argument("--no-cache", action="store_true")
    ingest.add_argument("--missing-only",action="store_true", help="Elabora soltanto articoli assenti in Neo4j")

    sub.add_parser("stats")
    
    p_pipe = sub.add_parser("pipeline", help="Esegue la pipeline di resolution")
    p_pipe.add_argument("--phase", default="all",
        choices=["all", "identity", "projection", "predicates", "events"])
    p_pipe.add_argument("--apply", action="store_true")
    p_pipe.add_argument("--verbose", action="store_true")
    p_pipe.add_argument("--save-state", action="store_true")
    p_pipe.add_argument("--out", default="output/resolution")
    p_pipe.add_argument(
        "--batch-id",
        default=None,
        help="Batch ValidatedFact della resolution (default: derivato dai modelli)",
    )

    p_enrich = sub.add_parser("enrich", help="Arricchisce il grafo")
    p_enrich.add_argument("--apply", action="store_true")

    p_norm = sub.add_parser("normalize-predicates", help="Normalizza predicati OTHER")
    p_norm.add_argument("--apply", action="store_true")

    p_quality = sub.add_parser("quality", help="Suite di qualità del grafo (audit/semantic/resolution/all), read-only")

    p_topics = sub.add_parser("topics", help="Topic semantici su EventMention (embedding, opzionale --apply)")
    args, unknown = parser.parse_known_args()

    if unknown and args.command != "quality":
        parser.error(f"argomenti non riconosciuti: {' '.join(unknown)}")

    if args.command == "preflight":
        run_preflight()

    elif args.command == "ingest":
        if args.missing_only and args.replace:
            parser.error("--missing-only e --replace non disponibili contemporaneamente:")
        run_ingest(
            args.limit,
            args.skip,
            args.replace,
            args.no_cache,
            args.missing_only,
        )
    elif args.command == "pipeline":
        from presskg.resolution import pipeline
        pipeline.run(args)
    elif args.command == "quality":
        from presskg.quality import audit
        rest = sys.argv[sys.argv.index("quality") + 1:]
        audit.main(argv=rest or ["--help"])
    elif args.command == "topics":
        from presskg.graph import topic_semantics
        rest = sys.argv[sys.argv.index("topics") + 1:]
        topic_semantics.main(argv=rest or ["--help"])
    elif args.command == "enrich":
        from argparse import Namespace
        from presskg.graph import enrich_graph
        if not enrich_graph.run(Namespace(apply=args.apply)):
            raise SystemExit(1)
    elif args.command == "normalize-predicates":
        from argparse import Namespace
        from presskg.graph import normalize_predicates
        if not normalize_predicates.run(Namespace(apply=args.apply)):
            raise SystemExit(1)
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