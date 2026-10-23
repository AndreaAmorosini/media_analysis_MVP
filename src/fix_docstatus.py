import json
from pathlib import Path

p = Path("rag_stampa/kv_store_doc_status.json")
data = json.loads(p.read_text(encoding="utf-8"))

n = 0
for rec in data.values():
    if "content_hash" in rec:
        rec.pop("content_hash")
        n += 1
print(f"rimossi {n} campi content_hash su {len(data)} record")

p.rename(p.with_suffix(".json.bak"))          # backup: non si sa mai
p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")