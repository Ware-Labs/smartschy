from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import canonical_net_name, cosine_similarity, hash_embedding, read_json, read_jsonl, tokenize


def resolve_project_root(project_root: Path | str) -> Path:
    root = Path(project_root).resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Invalid project root: {root}")
    return root


def _component_type(refdes: str) -> str:
    token = "".join(ch for ch in refdes.upper() if ch.isalpha())
    if token.startswith("U"):
        return "ic"
    if token.startswith("R"):
        return "resistor"
    if token.startswith("C"):
        return "capacitor"
    if token.startswith("L"):
        return "inductor"
    if token.startswith("D"):
        return "diode_led"
    if token.startswith("J"):
        return "connector"
    if token.startswith("P"):
        return "header"
    if token.startswith("TP"):
        return "test_point"
    if token.startswith("SW"):
        return "switch"
    if token.startswith("X"):
        return "crystal"
    return "other"


@dataclass
class DerivedArtifactStore:
    project_root: Path
    _cache: dict[str, Any]

    def __init__(self, project_root: Path | str):
        self.project_root = resolve_project_root(project_root)
        self._cache = {}

    @property
    def derived(self) -> Path:
        return self.project_root / "derived"

    def _load_json(self, rel_path: str, cache_key: str) -> dict[str, Any]:
        if cache_key not in self._cache:
            path = self.project_root / rel_path
            self._cache[cache_key] = read_json(path) if path.exists() else {}
        return self._cache[cache_key]

    def _load_jsonl(self, rel_path: str, cache_key: str) -> list[dict[str, Any]]:
        if cache_key not in self._cache:
            path = self.project_root / rel_path
            self._cache[cache_key] = read_jsonl(path) if path.exists() else []
        return self._cache[cache_key]

    @property
    def ingest_summary(self) -> dict[str, Any]:
        return self._load_json("derived/ingest_summary.json", "ingest_summary")

    @property
    def nets(self) -> list[dict[str, Any]]:
        return self._load_jsonl("derived/dsn/nets.jsonl", "nets")

    @property
    def pin_to_net(self) -> dict[str, dict[str, str]]:
        return self._load_json("derived/dsn/pin_to_net.json", "pin_to_net")

    @property
    def component_pin_index(self) -> dict[str, Any]:
        return self._load_json("derived/dsn/component_pin_index.json", "component_pin_index")

    @property
    def net_graph(self) -> dict[str, Any]:
        return self._load_json("derived/dsn/net_graph.json", "net_graph")

    @property
    def component_index(self) -> list[dict[str, Any]]:
        return self._load_jsonl("derived/bom/component_index.jsonl", "component_index")

    @property
    def refdes_to_part(self) -> dict[str, dict[str, Any]]:
        return self._load_json("derived/bom/refdes_to_part.json", "refdes_to_part")

    @property
    def pdf_chunks(self) -> list[dict[str, Any]]:
        return self._load_jsonl("derived/pdf/pdf_chunks.jsonl", "pdf_chunks")

    @property
    def pdf_chunk_manifest(self) -> dict[str, Any]:
        return self._load_json("derived/pdf/pdf_chunk_manifest.json", "pdf_chunk_manifest")

    @property
    def schematic_image_manifest(self) -> dict[str, Any]:
        return self._load_json("derived/pdf/schematic_page_images.json", "schematic_image_manifest")


def list_project_summary(project_root: Path | str) -> dict[str, Any]:
    store = DerivedArtifactStore(project_root)
    summary = store.ingest_summary
    counts = {
        "nets": len(store.nets),
        "pin_mappings": len(store.pin_to_net),
        "components_with_pins": len(store.component_pin_index.get("components", {})),
        "components_bom": len(store.refdes_to_part),
        "pdf_chunks": len(store.pdf_chunks),
        "schematic_images": int(store.schematic_image_manifest.get("page_count", 0)),
    }
    artifacts = {
        "ingest_summary": str(store.project_root / "derived" / "ingest_summary.json"),
        "dsn": {
            "nets": str(store.project_root / "derived" / "dsn" / "nets.jsonl"),
            "pin_to_net": str(store.project_root / "derived" / "dsn" / "pin_to_net.json"),
            "component_pin_index": str(store.project_root / "derived" / "dsn" / "component_pin_index.json"),
            "net_graph": str(store.project_root / "derived" / "dsn" / "net_graph.json"),
        },
        "bom": {
            "component_index": str(store.project_root / "derived" / "bom" / "component_index.jsonl"),
            "refdes_to_part": str(store.project_root / "derived" / "bom" / "refdes_to_part.json"),
        },
        "pdf": {
            "chunk_manifest": str(store.project_root / "derived" / "pdf" / "pdf_chunk_manifest.json"),
            "chunks": str(store.project_root / "derived" / "pdf" / "pdf_chunks.jsonl"),
            "schematic_images": str(store.project_root / "derived" / "pdf" / "schematic_page_images.json"),
        },
    }
    return {"project_root": str(store.project_root), "ingest_summary": summary, "counts": counts, "source_artifacts": artifacts}


def _search_blob_for_component(refdes: str, part_meta: dict[str, Any]) -> str:
    return " ".join(
        [
            refdes,
            part_meta.get("part_number", ""),
            part_meta.get("manufacturer", ""),
            part_meta.get("value", ""),
            part_meta.get("footprint", ""),
            part_meta.get("specification", ""),
            " ".join(part_meta.get("datasheet_candidates", [])),
        ]
    ).lower()


def search_components(project_root: Path | str, query: str, component_type: str | None = None) -> dict[str, Any]:
    store = DerivedArtifactStore(project_root)
    q = query.strip().lower()
    q_tokens = set(tokenize(query))
    rows: list[dict[str, Any]] = []
    for refdes, meta in store.refdes_to_part.items():
        bucket = _component_type(refdes)
        if component_type and bucket != component_type:
            continue
        blob = _search_blob_for_component(refdes, meta)
        score = 0.0
        if refdes.lower() == q:
            score += 4.0
        if q and q in blob:
            score += 2.0
        score += sum(0.3 for token in q_tokens if token in blob)
        if score <= 0:
            continue
        rows.append(
            {
                "refdes": refdes,
                "component_type": bucket,
                "score": round(score, 3),
                "part_number": meta.get("part_number", ""),
                "manufacturer": meta.get("manufacturer", ""),
                "value": meta.get("value", ""),
                "footprint": meta.get("footprint", ""),
                "datasheet_candidates": meta.get("datasheet_candidates", []),
            }
        )
    rows.sort(key=lambda row: (row["score"], row["refdes"]), reverse=True)
    return {"query": query, "component_type_filter": component_type, "matches": rows, "source_artifacts": ["derived/bom/refdes_to_part.json"]}


def get_component(project_root: Path | str, refdes: str) -> dict[str, Any]:
    store = DerivedArtifactStore(project_root)
    key = refdes.upper()
    part = store.refdes_to_part.get(key)
    if not part:
        raise KeyError(f"Unknown component: {key}")
    pin_index = store.component_pin_index.get("components", {}).get(key, {})
    return {
        "refdes": key,
        "component_type": _component_type(key),
        "bom_metadata": part,
        "pins_summary": {
            "all_pin_count": len(pin_index.get("all_pins", [])),
            "connected_pin_count": len(pin_index.get("connected_pins", [])),
            "floating_pin_count": len(pin_index.get("floating_pins", [])),
        },
        "source_artifacts": [
            "derived/bom/refdes_to_part.json",
            "derived/dsn/component_pin_index.json",
        ],
    }


def get_component_pins(project_root: Path | str, refdes: str) -> dict[str, Any]:
    store = DerivedArtifactStore(project_root)
    key = refdes.upper()
    comp = store.component_pin_index.get("components", {}).get(key)
    if not comp:
        raise KeyError(f"Unknown component pin index: {key}")
    net_map: dict[str, str] = {}
    for pin in comp.get("connected_pins", []):
        pin_token = f"{key}-{pin}"
        net_payload = store.pin_to_net.get(pin_token)
        if net_payload:
            net_map[str(pin)] = net_payload.get("net_name_canonical", "")
    return {
        "refdes": key,
        "all_pins": comp.get("all_pins", []),
        "connected_pins": comp.get("connected_pins", []),
        "floating_pins": comp.get("floating_pins", []),
        "connected_pin_nets": net_map,
        "source_artifacts": [
            "derived/dsn/component_pin_index.json",
            "derived/dsn/pin_to_net.json",
        ],
    }


def get_pin_net(project_root: Path | str, refdes: str, pin: str) -> dict[str, Any]:
    store = DerivedArtifactStore(project_root)
    token = f"{refdes.upper()}-{pin}"
    net = store.pin_to_net.get(token)
    if not net:
        return {
            "refdes": refdes.upper(),
            "pin": str(pin),
            "connected": False,
            "net_name_canonical": None,
            "net_name_raw": None,
            "source_artifact": "derived/dsn/pin_to_net.json",
        }
    return {
        "refdes": refdes.upper(),
        "pin": str(pin),
        "connected": True,
        "net_name_canonical": net.get("net_name_canonical"),
        "net_name_raw": net.get("net_name_raw"),
        "source_artifact": "derived/dsn/pin_to_net.json",
    }


def _net_alias_lookup(store: DerivedArtifactStore) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for row in store.nets:
        canonical = row.get("net_name_canonical", "")
        aliases = row.get("aliases", []) + [row.get("net_name_raw", "")]
        for alias in aliases:
            lookup[canonical_net_name(alias)] = canonical
    return lookup


def search_nets(project_root: Path | str, query: str) -> dict[str, Any]:
    store = DerivedArtifactStore(project_root)
    needle = canonical_net_name(query)
    hits = []
    for row in store.nets:
        canonical = row.get("net_name_canonical", "")
        aliases = row.get("aliases", []) + [row.get("net_name_raw", "")]
        reason = ""
        score = 0.0
        if canonical == needle:
            score = 4.0
            reason = "exact_canonical"
        elif any(canonical_net_name(alias) == needle for alias in aliases):
            score = 3.0
            reason = "exact_alias"
        elif needle and (needle in canonical or canonical in needle):
            score = 2.0
            reason = "partial_match"
        if score:
            hits.append(
                {
                    "net_name_canonical": canonical,
                    "net_name_raw": row.get("net_name_raw", ""),
                    "score": score,
                    "match_reason": reason,
                }
            )
    hits.sort(key=lambda item: (item["score"], item["net_name_canonical"]), reverse=True)
    return {"query": query, "matches": hits, "source_artifact": "derived/dsn/nets.jsonl"}


def get_net(project_root: Path | str, net_name_or_alias: str) -> dict[str, Any]:
    store = DerivedArtifactStore(project_root)
    lookup = _net_alias_lookup(store)
    resolved = lookup.get(canonical_net_name(net_name_or_alias), canonical_net_name(net_name_or_alias))
    row = next((item for item in store.nets if item.get("net_name_canonical") == resolved), None)
    if not row:
        raise KeyError(f"Unknown net: {net_name_or_alias}")
    members = _build_members_from_net(row)
    return {
        "resolved_from": net_name_or_alias,
        "net": row,
        "members": members,
        "source_artifact": "derived/dsn/nets.jsonl",
    }


def _build_members_from_net(net_row: dict[str, Any]) -> dict[str, Any]:
    grouped: dict[str, list[str]] = {}
    for token in net_row.get("pins_raw", []):
        if "-" not in token:
            continue
        refdes, pin = token.rsplit("-", 1)
        grouped.setdefault(refdes.upper(), []).append(pin)
    for pins in grouped.values():
        pins.sort()
    return {"component_to_pins": grouped, "component_count": len(grouped), "pin_count": len(net_row.get("pins_raw", []))}


def get_net_members(project_root: Path | str, net_name: str) -> dict[str, Any]:
    net_payload = get_net(project_root, net_name)
    return {
        "net_name_canonical": net_payload["net"]["net_name_canonical"],
        "net_name_raw": net_payload["net"]["net_name_raw"],
        "members": net_payload["members"],
        "source_artifact": "derived/dsn/nets.jsonl",
    }


def trace_net_neighborhood(
    project_root: Path | str,
    seed_nets: list[str] | None = None,
    seed_pins: list[str] | None = None,
    seed_components: list[str] | None = None,
    depth: int = 1,
    max_nodes: int = 250,
) -> dict[str, Any]:
    store = DerivedArtifactStore(project_root)
    alias_lookup = _net_alias_lookup(store)
    adjacency = store.net_graph.get("adjacency", {})
    seed_nodes: list[str] = []
    for net in seed_nets or []:
        canonical = alias_lookup.get(canonical_net_name(net), canonical_net_name(net))
        seed_nodes.append(f"net:{canonical}")
    for pin in seed_pins or []:
        token = pin.upper()
        if not token.startswith("PIN:"):
            token = f"pin:{token}"
        seed_nodes.append(token)
    for refdes in seed_components or []:
        seed_nodes.append(f"component:{refdes.upper()}")
    visited: set[str] = set(seed_nodes)
    q = deque((node, 0) for node in seed_nodes)
    edges: list[dict[str, str]] = []
    truncated = False
    while q:
        node, d = q.popleft()
        if d >= depth:
            continue
        for nxt in adjacency.get(node, []):
            edges.append({"src": node, "dst": nxt})
            if len(visited) >= max_nodes and nxt not in visited:
                truncated = True
                continue
            if nxt not in visited:
                visited.add(nxt)
                q.append((nxt, d + 1))
    node_lookup = {n["id"]: n for n in store.net_graph.get("nodes", [])}
    sub_nodes = [node_lookup[node_id] for node_id in visited if node_id in node_lookup]
    return {
        "seed_nodes": seed_nodes,
        "depth": depth,
        "max_nodes": max_nodes,
        "truncated": truncated,
        "node_count": len(sub_nodes),
        "edge_count": len(edges),
        "nodes": sub_nodes,
        "edges": edges,
        "source_artifact": "derived/dsn/net_graph.json",
    }


def _chunk_lexical_score(tokens: list[str], token_set: set[str]) -> float:
    counts = Counter(tokens)
    return sum(counts.get(token, 0) for token in token_set)


def _search_chunks(
    chunks: list[dict[str, Any]],
    query: str,
    max_results: int,
    source_type: str = "any",
    source_file_allow: set[str] | None = None,
) -> list[dict[str, Any]]:
    q_tokens = tokenize(query)
    token_set = set(q_tokens)
    q_vec = hash_embedding(q_tokens, dims=512)
    rows: list[dict[str, Any]] = []
    for chunk in chunks:
        chunk_type = chunk.get("source_type", "")
        if source_type != "any" and chunk_type != source_type:
            continue
        if source_file_allow is not None and chunk.get("source_file") not in source_file_allow:
            continue
        lex = _chunk_lexical_score(chunk.get("tokens", []), token_set)
        sem = cosine_similarity(q_vec, hash_embedding(chunk.get("tokens", []), dims=512))
        score = lex + 0.35 * sem
        if score <= 0:
            continue
        rows.append(
            {
                "chunk_id": chunk.get("chunk_id"),
                "source_type": chunk_type,
                "source_file": chunk.get("source_file"),
                "page_start": chunk.get("page_start"),
                "page_end": chunk.get("page_end"),
                "heading_path": chunk.get("heading_path", []),
                "score": round(score, 6),
                "snippet": str(chunk.get("text", ""))[:400],
                "tokens": chunk.get("tokens", [])[:80],
            }
        )
    rows.sort(key=lambda row: row["score"], reverse=True)
    return rows[: max(0, max_results)]


def search_pdf_chunks(
    project_root: Path | str,
    query: str,
    source_type: str = "any",
    refdes: str | None = None,
    mpn: str | None = None,
    max_results: int = 10,
) -> dict[str, Any]:
    store = DerivedArtifactStore(project_root)
    allow: set[str] | None = None
    if refdes:
        comp = store.refdes_to_part.get(refdes.upper(), {})
        candidates = comp.get("datasheet_candidates", [])
        if candidates:
            allow = set(candidates)
    elif mpn:
        token = mpn.lower()
        allow = {chunk.get("source_file", "") for chunk in store.pdf_chunks if token in str(chunk.get("source_file", "")).lower()}
    rows = _search_chunks(store.pdf_chunks, query, max_results=max_results, source_type=source_type, source_file_allow=allow)
    return {
        "query": query,
        "source_type": source_type,
        "refdes_filter": refdes,
        "mpn_filter": mpn,
        "max_results": max_results,
        "results": rows,
        "source_artifact": "derived/pdf/pdf_chunks.jsonl",
    }


def get_pdf_chunk(project_root: Path | str, chunk_id: str) -> dict[str, Any]:
    store = DerivedArtifactStore(project_root)
    row = next((chunk for chunk in store.pdf_chunks if chunk.get("chunk_id") == chunk_id), None)
    if row is None:
        raise KeyError(f"Unknown chunk id: {chunk_id}")
    return {"chunk": row, "source_artifact": "derived/pdf/pdf_chunks.jsonl"}


def find_datasheets_for_component(project_root: Path | str, refdes: str) -> dict[str, Any]:
    store = DerivedArtifactStore(project_root)
    key = refdes.upper()
    part = store.refdes_to_part.get(key)
    if not part:
        raise KeyError(f"Unknown component: {key}")
    candidates = part.get("datasheet_candidates", [])
    reasons = [{"filename": candidate, "reason": "bom_part_number_match"} for candidate in candidates]
    return {"refdes": key, "datasheet_candidates": candidates, "match_reasons": reasons, "source_artifact": "derived/bom/refdes_to_part.json"}


def search_datasheet_chunks(project_root: Path | str, refdes: str, query: str, max_results: int = 8) -> dict[str, Any]:
    datasheets = find_datasheets_for_component(project_root, refdes).get("datasheet_candidates", [])
    store = DerivedArtifactStore(project_root)
    allow = set(datasheets) if datasheets else None
    rows = _search_chunks(store.pdf_chunks, query, max_results=max_results, source_type="datasheet", source_file_allow=allow)
    return {
        "refdes": refdes.upper(),
        "query": query,
        "max_results": max_results,
        "results": rows,
        "source_artifact": "derived/pdf/pdf_chunks.jsonl",
    }


def get_schematic_pages(
    project_root: Path | str,
    query: str | None = None,
    refdes: str | None = None,
    net: str | None = None,
    max_results: int = 6,
) -> dict[str, Any]:
    store = DerivedArtifactStore(project_root)
    manifest = store.schematic_image_manifest
    images = manifest.get("images", [])
    page_reasons: list[dict[str, Any]] = []
    if query:
        results = _search_chunks(store.pdf_chunks, query, max_results=max_results, source_type="schematic")
        for row in results:
            page_reasons.append(
                {
                    "page_number": row.get("page_start"),
                    "reason": "query_chunk_match",
                    "chunk_id": row.get("chunk_id"),
                    "score": row.get("score"),
                }
            )
    if refdes:
        results = _search_chunks(store.pdf_chunks, refdes, max_results=max_results, source_type="schematic")
        for row in results:
            page_reasons.append(
                {
                    "page_number": row.get("page_start"),
                    "reason": "refdes_chunk_match",
                    "chunk_id": row.get("chunk_id"),
                    "score": row.get("score"),
                }
            )
    if net:
        results = _search_chunks(store.pdf_chunks, net, max_results=max_results, source_type="schematic")
        for row in results:
            page_reasons.append(
                {
                    "page_number": row.get("page_start"),
                    "reason": "net_chunk_match",
                    "chunk_id": row.get("chunk_id"),
                    "score": row.get("score"),
                }
            )
    dedup_pages = sorted({int(item.get("page_number")) for item in page_reasons if isinstance(item.get("page_number"), int)})
    return {
        "schematic_pdf": manifest.get("schematic_pdf", ""),
        "available_pages": [row.get("page_number") for row in images],
        "images": images,
        "relevant_pages": dedup_pages,
        "relevance_reasons": page_reasons,
        "source_artifacts": [
            "derived/pdf/schematic_page_images.json",
            "derived/pdf/pdf_chunks.jsonl",
        ],
    }


def get_schematic_page_image(project_root: Path | str, page_number: int, include_bytes: bool = False) -> dict[str, Any]:
    store = DerivedArtifactStore(project_root)
    manifest = store.schematic_image_manifest
    image = next((row for row in manifest.get("images", []) if row.get("page_number") == page_number), None)
    if image is None:
        raise KeyError(f"No image for schematic page {page_number}")
    image_path = store.project_root / "derived" / "pdf" / str(image.get("image_path", ""))
    payload = {
        "page_number": page_number,
        "image_path": str(image_path),
        "width": image.get("width"),
        "height": image.get("height"),
        "schematic_pdf": manifest.get("schematic_pdf"),
        "source_artifact": "derived/pdf/schematic_page_images.json",
    }
    if include_bytes:
        payload["image_bytes"] = image_path.read_bytes()
    return payload


def get_component_context_bundle(
    project_root: Path | str,
    refdes: str,
    query: str | None = None,
    max_results: int = 6,
) -> dict[str, Any]:
    key = refdes.upper()
    base_query = query or key
    component = get_component(project_root, key)
    pins = get_component_pins(project_root, key)
    datasheets = find_datasheets_for_component(project_root, key)
    datasheet_chunks = search_datasheet_chunks(project_root, key, base_query, max_results=max_results)
    schematic_pages = get_schematic_pages(project_root, query=base_query, refdes=key, max_results=max_results)
    related_nets = sorted(set(pins.get("connected_pin_nets", {}).values()))
    return {
        "refdes": key,
        "component": component,
        "pins": pins,
        "related_nets": related_nets,
        "datasheets": datasheets,
        "top_datasheet_chunks": datasheet_chunks.get("results", []),
        "related_schematic_pages": schematic_pages.get("relevant_pages", []),
        "source_artifacts": [
            "derived/bom/refdes_to_part.json",
            "derived/dsn/component_pin_index.json",
            "derived/dsn/pin_to_net.json",
            "derived/pdf/pdf_chunks.jsonl",
            "derived/pdf/schematic_page_images.json",
        ],
    }


def build_evidence_packet(
    project_root: Path | str,
    question: str,
    selected_evidence: list[dict[str, Any]],
    agent_trace: dict[str, Any] | None = None,
    resolved_entities: dict[str, Any] | None = None,
    open_uncertainties: list[str] | None = None,
    critical_findings: list[str] | None = None,
    recommended_answer_constraints: list[str] | None = None,
    limits: dict[str, Any] | None = None,
    stop_reason: str | None = None,
) -> dict[str, Any]:
    from .evidence_packet import build_evidence_packet as _build

    return _build(
        project_root=resolve_project_root(project_root),
        question=question,
        selected_evidence=selected_evidence,
        agent_trace=agent_trace,
        resolved_entities=resolved_entities,
        open_uncertainties=open_uncertainties,
        critical_findings=critical_findings,
        recommended_answer_constraints=recommended_answer_constraints,
        limits=limits,
        stop_reason=stop_reason,
    )


def render_prompt_from_evidence_packet(
    project_root: Path | str,
    evidence_packet: dict[str, Any],
) -> dict[str, Any]:
    from .prompt_render import render_prompt_from_evidence_packet as _render

    _ = resolve_project_root(project_root)
    prompt = _render(evidence_packet)
    return {"prompt_text": prompt}
