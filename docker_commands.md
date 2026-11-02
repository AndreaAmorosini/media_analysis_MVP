docker compose exec -T presskg pixi run ingest --replace

docker compose exec presskg pixi run python tests/kg_quality.py all --random 300 --diagnostic 150 --seed 42 --max-tokens 1500 --timeout 600 --retries 2 --judge-context-chars 550  --out output/evaluations/qwen38_gemma31_300

docker compose exec -T presskg pixi run python scripts/validate_claims.py generate --limit 0 --context-chars 600 --max-tokens 1600 --retries 1 --resume --out output/semantic_validation/gemma31_primary.csv

docker compose exec -T presskg pixi run python scripts/auto_apply_claim_validation.py apply --primary output/semantic_validation/gemma31_primary.csv --batch-id qwen38-validation-v1 --limit 100

docker compose exec -T presskg bash scripts/run_validation_full.sh



FORCE UNSLOTH UNLOAD

curl -sS -X POST   http://127.0.0.1:8081/api/inference/unload   -H "Content-Type: application/json"   -H "Authorization: Bearer sk-unsloth-d62bd8f2cc4a38cb236c4d3b5579a532"   -d '{
    "model_path": "unsloth/gemma-4-31B-it-GGUF:Q6_K",
    "force_cancel_active": true
  }' | jq