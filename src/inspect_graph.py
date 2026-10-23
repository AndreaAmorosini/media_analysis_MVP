import networkx as nx
from pyvis.network import Network

from config import WORKDIR, OUT_DIR

GRAPHML = WORKDIR / "graph_chunk_entity_relation.graphml"

G = nx.read_graphml(GRAPHML)
G_und = G.to_undirected()

print(f"Nodi (entità):      {G.number_of_nodes()}")
print(f"Archi (relazioni):  {G.number_of_edges()}")

deg = sorted(G_und.degree, key=lambda x: x[1], reverse=True)
print("\nTop 20 entità per grado:")
for node, d in deg[:20]:
    print(f"  {d:>3}  {node}")

components = sorted((len(c) for c in nx.connected_components(G_und)), reverse=True)
print(f"\nComponenti connesse: {len(components)} (taglie: {components[:10]})")

OUT_DIR.mkdir(exist_ok=True)
net = Network(height="900px", width="100%", bgcolor="#111", font_color="#eee", notebook=False)
net.from_nx(G_und)

PALETTE = {
    "person": "#e74c3c", "organization": "#3498db", "location": "#2ecc71",
    "event": "#f39c12", "category": "#9b59b6", "product": "#1abc9c",
}
for n in net.nodes:
    data = G_und.nodes[n["id"]]
    etype = str(data.get("entity_type", "")).lower()
    n["color"] = PALETTE.get(etype, "#95a5a6")
    n["title"] = (data.get("description", "") or "")[:300]   # hover = descrizione
    n["size"] = 8 + min(30, 3 * G_und.degree(n["id"]))       # dimensione ∝ grado

net.write_html(str(OUT_DIR / "kg_rassegna.html"), open_browser=False)
print(f"HTML interattivo: {OUT_DIR / 'kg_rassegna.html'}")