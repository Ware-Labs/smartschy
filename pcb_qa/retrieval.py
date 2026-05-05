from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
import re

from .utils import (
    canonical_net_name,
    cosine_similarity,
    hash_embedding,
    read_json,
    read_jsonl,
    tokenize,
)


ROLE_ALIASES: dict[str, set[str]] = {
    "microcontroller": {
        "mcu",
        "microcontroller",
        "soc",
        "nrf54l15",
        "mod1",
        "bluetooth module",
    },
    "crystal": {
        "crystal",
        "xtal",
        "lfxtal",
        "32.768khz",
        "32k",
        "x1",
        "lf clock",
        "low frequency oscillator",
    },
    "module": {"module", "radio module", "bluetooth module"},
    "imu": {"imu", "accelerometer", "gyroscope"},
    "usb_connector": {"usb", "usb-c", "type-c", "connector"},
    "test_point": {"test point", "test points", "tp"},
    "i2c": {"i2c", "scl", "sda"},
}


@dataclass
class RetrievedEvidence:
    source_type: str
    source_id: str
    score: float
    payload: dict


@dataclass
class RetrievalResult:
    entities: dict[str, list[str]]
    net_evidence: list[RetrievedEvidence]
    datasheet_evidence: list[RetrievedEvidence]
    schematic_evidence: list[RetrievedEvidence]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _extract_symbol_like(question: str) -> set[str]:
    candidates = set()
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.\\/-]*", question):
        if len(token) >= 2 and re.search(r"[A-Za-z]", token):
            candidates.add(token)
    for token in re.findall(r"\b[A-Za-z]+[0-9]+\b", question):
        candidates.add(token)
    return candidates


class QueryParser:
    def __init__(self, refdes_to_part: dict[str, dict], nets: list[dict]):
        self.refdes_to_part = refdes_to_part
        self.nets = nets
        self.known_refdes = {key.upper() for key in refdes_to_part}
        self.net_aliases = {
            alias.upper()
            for net in nets
            for alias in net.get("aliases", []) + [net.get("net_name_raw", "")]
        }
        self.refdes_by_role = self._build_refdes_by_role()
        self.net_names = [n.get("net_name_canonical", "") for n in nets]

    def _build_refdes_by_role(self) -> dict[str, list[str]]:
        role_map: dict[str, list[str]] = defaultdict(list)
        for refdes, meta in self.refdes_to_part.items():
            stacked = " ".join(
                [
                    refdes,
                    meta.get("part_number", ""),
                    meta.get("value", ""),
                    meta.get("specification", ""),
                    meta.get("manufacturer", ""),
                ]
            ).lower()
            norm = _norm(stacked)
            if (
                "nrf54l15" in norm
                or "transceivermodule" in norm
                or refdes.upper().startswith("MOD")
            ):
                role_map["microcontroller"].append(refdes)
                role_map["module"].append(refdes)
            if (
                refdes.upper().startswith("X")
                or "32.768" in stacked
                or "crystal" in stacked
                or "xtal" in stacked
            ):
                role_map["crystal"].append(refdes)
            if refdes.upper().startswith("U3") or "icm-42605" in norm:
                role_map["imu"].append(refdes)
            if refdes.upper().startswith("J4") or "usb" in norm:
                role_map["usb_connector"].append(refdes)
            if refdes.upper().startswith("TP"):
                role_map["test_point"].append(refdes)
        return role_map

    def _infer_nets_from_text(self, q_lower: str) -> set[str]:
        inferred: set[str] = set()
        if "i2c" in q_lower or "scl" in q_lower or "sda" in q_lower:
            for net in self.net_names:
                if "I2C" in net or net.endswith("_SCL") or net.endswith("_SDA"):
                    inferred.add(net)
        if "imu" in q_lower:
            for net in self.net_names:
                if net.startswith("IMU_"):
                    inferred.add(net)
        if "usb" in q_lower:
            for net in self.net_names:
                if "VBUS" in net or net.startswith("NETJ4_"):
                    inferred.add(net)
        if "gpio" in q_lower:
            for net in self.net_names:
                if net.startswith("P0.") or net.startswith("P1.") or net.startswith("P2."):
                    inferred.add(net)
        return inferred

    def parse(self, question: str) -> dict[str, list[str]]:
        q_lower = question.lower()
        symbols = _extract_symbol_like(question)
        matched_refdes = sorted(
            {token.upper() for token in symbols if token.upper() in self.known_refdes}
        )
        matched_nets = sorted(
            {
                canonical_net_name(token)
                for token in symbols
                if token.upper() in self.net_aliases
            }
        )
        for token in symbols:
            token_canon = canonical_net_name(token)
            if len(token_canon) < 4:
                continue
            for alias_canon in self.net_aliases:
                if len(alias_canon) < 4:
                    continue
                if token_canon in alias_canon or alias_canon in token_canon:
                    matched_nets.append(alias_canon)
        roles: set[str] = set()
        for role, aliases in ROLE_ALIASES.items():
            if any(alias in q_lower for alias in aliases):
                roles.add(role)
        for role in sorted(roles):
            matched_refdes.extend(self.refdes_by_role.get(role, []))
        matched_nets.extend(self._infer_nets_from_text(q_lower))
        return {
            "roles": sorted(roles),
            "refdes": sorted(set(matched_refdes)),
            "nets": sorted(set(matched_nets)),
            "symbols": sorted(symbols),
        }


class HybridRetriever:
    def __init__(self, project_root: Path):
        derived = project_root / "derived"
        self.nets = read_jsonl(derived / "dsn" / "nets.jsonl")
        self.pin_to_net = read_json(derived / "dsn" / "pin_to_net.json")
        self.net_graph = read_json(derived / "dsn" / "net_graph.json")
        self.refdes_to_part = read_json(derived / "bom" / "refdes_to_part.json")
        self.pdf_chunks = read_jsonl(derived / "pdf" / "pdf_chunks.jsonl")
        self.parser = QueryParser(self.refdes_to_part, self.nets)
        self.net_lookup = {n["net_name_canonical"]: n for n in self.nets}
        self.net_by_alias = {}
        for net in self.nets:
            canonical = net["net_name_canonical"]
            for alias in net.get("aliases", []):
                self.net_by_alias[canonical_net_name(alias)] = canonical
            self.net_by_alias[canonical_net_name(net["net_name_raw"])] = canonical
        self.adj = self.net_graph.get("adjacency", {})
        self.chunk_tokens = [set(chunk.get("tokens", [])) for chunk in self.pdf_chunks]
        self.chunk_vecs = [
            hash_embedding(chunk.get("tokens", []), dims=512) for chunk in self.pdf_chunks
        ]

    def _nets_for_refdes(self, refdes: str) -> set[str]:
        needle = f"{refdes.upper()}-"
        nets = set()
        for net in self.nets:
            for token in net.get("pins_raw", []):
                if token.upper().startswith(needle):
                    nets.add(net["net_name_canonical"])
                    break
        return nets

    def _walk_from_net(self, canonical_net: str, depth: int) -> set[str]:
        start = f"net:{canonical_net}"
        visited = {start}
        queue = deque([(start, 0)])
        out: set[str] = set()
        while queue:
            node, node_depth = queue.popleft()
            if node.startswith("net:"):
                out.add(node.replace("net:", "", 1))
            if node_depth >= depth:
                continue
            for nxt in self.adj.get(node, []):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append((nxt, node_depth + 1))
        return out

    def _net_evidence(self, entities: dict[str, list[str]], depth: int) -> list[RetrievedEvidence]:
        seed_nets: set[str] = set()
        for net in entities.get("nets", []):
            canonical = self.net_by_alias.get(canonical_net_name(net), canonical_net_name(net))
            if canonical in self.net_lookup:
                seed_nets.add(canonical)
        for refdes in entities.get("refdes", []):
            seed_nets.update(self._nets_for_refdes(refdes))
        expanded: set[str] = set(seed_nets)
        for net in list(seed_nets):
            expanded.update(self._walk_from_net(net, depth))
        ranked = []
        for net in expanded:
            record = self.net_lookup.get(net)
            if not record:
                continue
            pin_hits = 0
            for token in record.get("pins_raw", []):
                refdes = token.split("-", 1)[0].upper()
                if refdes in entities.get("refdes", []):
                    pin_hits += 1
            score = 2.0 if net in seed_nets else 1.0
            score += 0.5 * pin_hits
            ranked.append(
                RetrievedEvidence(
                    source_type="net",
                    source_id=net,
                    score=score,
                    payload=record,
                )
            )
        ranked.sort(key=lambda row: row.score, reverse=True)
        return ranked

    def _candidate_datasheets(self, entities: dict[str, list[str]]) -> set[str]:
        allowed = set()
        for refdes in entities.get("refdes", []):
            meta = self.refdes_to_part.get(refdes, {})
            for filename in meta.get("datasheet_candidates", []):
                allowed.add(filename)
        # Role fallback: if no explicit refdes hit, widen to obvious candidates.
        if "microcontroller" in entities.get("roles", []) and not allowed:
            for refdes in self.parser.refdes_by_role.get("microcontroller", []):
                meta = self.refdes_to_part.get(refdes, {})
                for filename in meta.get("datasheet_candidates", []):
                    allowed.add(filename)
        if "crystal" in entities.get("roles", []) and not allowed:
            for refdes in self.parser.refdes_by_role.get("crystal", []):
                meta = self.refdes_to_part.get(refdes, {})
                for filename in meta.get("datasheet_candidates", []):
                    allowed.add(filename)
        return allowed

    def _chunk_lexical_score(self, tokens: list[str], token_set: set[str]) -> float:
        if not tokens:
            return 0.0
        counts = Counter(tokens)
        score = 0.0
        for token in token_set:
            score += counts.get(token, 0)
        return score

    def _pdf_evidence(
        self,
        question: str,
        entities: dict[str, list[str]],
        top_k: int,
    ) -> tuple[list[RetrievedEvidence], list[RetrievedEvidence]]:
        q_tokens = tokenize(question)
        q_token_set = set(q_tokens)
        q_vec = hash_embedding(q_tokens, dims=512)
        allowed_datasheets = self._candidate_datasheets(entities)
        scored: list[RetrievedEvidence] = []
        for idx, chunk in enumerate(self.pdf_chunks):
            source_type = chunk.get("source_type", "")
            if source_type == "datasheet":
                if allowed_datasheets and chunk.get("source_file") not in allowed_datasheets:
                    continue
            lex = self._chunk_lexical_score(chunk.get("tokens", []), q_token_set)
            sem = cosine_similarity(q_vec, self.chunk_vecs[idx])
            # Exact-first gate: require at least one lexical hit for datasheets unless
            # they are in a strongly constrained candidate set.
            if source_type == "datasheet" and not lex and not allowed_datasheets:
                continue
            score = lex + (0.35 * sem)
            heading_blob = " ".join(chunk.get("heading_path", [])).lower()
            if any(tag in heading_blob for tag in ["pin", "function", "power", "timing", "osc"]):
                score += 0.4
            scored.append(
                RetrievedEvidence(
                    source_type=source_type,
                    source_id=chunk.get("chunk_id", f"chunk:{idx}"),
                    score=score,
                    payload=chunk,
                )
            )
        scored.sort(key=lambda row: row.score, reverse=True)
        datasheet = [row for row in scored if row.source_type == "datasheet"][:top_k]
        schematic = [row for row in scored if row.source_type == "schematic"][: max(2, top_k // 2)]
        return datasheet, schematic

    def retrieve(self, question: str, net_walk_depth: int = 1, top_k: int = 6) -> RetrievalResult:
        entities = self.parser.parse(question)
        net_evidence = self._net_evidence(entities, depth=net_walk_depth)
        datasheet_evidence, schematic_evidence = self._pdf_evidence(question, entities, top_k=top_k)
        return RetrievalResult(
            entities=entities,
            net_evidence=net_evidence,
            datasheet_evidence=datasheet_evidence,
            schematic_evidence=schematic_evidence,
        )
