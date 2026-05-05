from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
import json
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


LEGACY_ROLE_ALIASES: dict[str, set[str]] = {
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
    def __init__(
        self,
        refdes_to_part: dict[str, dict],
        nets: list[dict],
        project_root: Path,
        resolver_mode: str = "config",
    ):
        if resolver_mode not in {"config", "legacy"}:
            raise ValueError(f"Unsupported resolver_mode={resolver_mode!r}")
        self.resolver_mode = resolver_mode
        self.refdes_to_part = refdes_to_part
        self.nets = nets
        self.known_refdes = {key.upper() for key in refdes_to_part}
        self.project_root = project_root
        self.net_aliases = {
            alias.upper()
            for net in nets
            for alias in net.get("aliases", []) + [net.get("net_name_raw", "")]
        }
        self.refdes_to_nets = self._build_refdes_to_nets()
        self.role_config = self._load_role_config()
        self.refdes_by_role_legacy = self._build_refdes_by_role_legacy()
        self.role_catalog = self._build_role_candidate_catalog()
        self.net_names = [n.get("net_name_canonical", "") for n in nets]

    def _build_refdes_to_nets(self) -> dict[str, set[str]]:
        mapping: dict[str, set[str]] = defaultdict(set)
        for net in self.nets:
            net_name = net.get("net_name_canonical", "")
            for token in net.get("pins_raw", []):
                refdes = token.split("-", 1)[0].upper()
                mapping[refdes].add(net_name)
        return mapping

    def _load_role_config(self) -> dict[str, dict]:
        base = self.project_root / "profiles" / "default_roles.json"
        merged: dict[str, dict] = {"roles": {}}
        if base.exists():
            payload = json.loads(base.read_text(encoding="utf-8"))
            merged["roles"].update(payload.get("roles", {}))
        project_override = self.project_root / "profiles" / "project_roles.json"
        if project_override.exists():
            payload = json.loads(project_override.read_text(encoding="utf-8"))
            for role, cfg in payload.get("roles", {}).items():
                existing = dict(merged["roles"].get(role, {}))
                for key, value in cfg.items():
                    if key in {"aliases", "signals", "refdes_prefixes"}:
                        existing[key] = sorted(set(existing.get(key, []) + value))
                    else:
                        existing[key] = value
                merged["roles"][role] = existing
        if not merged["roles"]:
            merged["roles"] = {
                role: {"aliases": sorted(aliases), "signals": [], "refdes_prefixes": []}
                for role, aliases in LEGACY_ROLE_ALIASES.items()
            }
        return merged

    def _build_refdes_by_role_legacy(self) -> dict[str, list[str]]:
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

    def _score_refdes_for_role(self, refdes: str, meta: dict, role_cfg: dict) -> float:
        score = 0.0
        stacked = " ".join(
            [
                refdes,
                meta.get("part_number", ""),
                meta.get("value", ""),
                meta.get("specification", ""),
                meta.get("manufacturer", ""),
                " ".join(meta.get("datasheet_candidates", [])),
            ]
        ).lower()
        aliases = [a.lower() for a in role_cfg.get("aliases", [])]
        signals = [s.lower() for s in role_cfg.get("signals", [])]
        prefixes = [p.upper() for p in role_cfg.get("refdes_prefixes", [])]
        negatives = [n.lower() for n in role_cfg.get("negative_terms", [])]

        for token in aliases:
            if token and token in stacked:
                score += 1.2
        for token in signals:
            if token and token in stacked:
                score += 1.0
        for token in negatives:
            if token and token in stacked:
                score -= 1.3
        refdes_upper = refdes.upper()
        for prefix in prefixes:
            if prefix and refdes_upper.startswith(prefix):
                score += 0.8
        # Net-rich entities are weakly preferred when roles describe interfaces.
        if self.refdes_to_nets.get(refdes_upper):
            score += min(0.6, 0.05 * len(self.refdes_to_nets[refdes_upper]))
        return score

    def _build_role_candidate_catalog(self) -> dict[str, list[dict[str, float | str]]]:
        catalog: dict[str, list[dict[str, float | str]]] = {}
        roles = self.role_config.get("roles", {})
        for role, cfg in roles.items():
            candidates: list[dict[str, float | str]] = []
            for refdes, meta in self.refdes_to_part.items():
                score = self._score_refdes_for_role(refdes, meta, cfg)
                if score > 0.15:
                    candidates.append({"refdes": refdes, "score": round(score, 4)})
            candidates.sort(key=lambda row: float(row["score"]), reverse=True)
            catalog[role] = candidates
        return catalog

    def _score_to_band(self, score: float) -> str:
        if score >= 2.8:
            return "high"
        if score >= 1.4:
            return "medium"
        return "low"

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

    def _infer_nets_from_topology(
        self,
        roles: set[str],
        selected_refdes: set[str],
    ) -> set[str]:
        inferred: set[str] = set()
        if "crystal" in roles and ("microcontroller" in roles or "module" in roles):
            crystal_refs = {
                row["refdes"].upper() for row in self.role_catalog.get("crystal", [])[:5]
            }
            controller_refs = {
                row["refdes"].upper()
                for row in (
                    self.role_catalog.get("microcontroller", [])[:5]
                    + self.role_catalog.get("module", [])[:5]
                )
            }
            crystal_refs.update({ref for ref in selected_refdes if ref.startswith("X")})
            for net in self.nets:
                net_name = net.get("net_name_canonical", "")
                net_refdes = {token.split("-", 1)[0].upper() for token in net.get("pins_raw", [])}
                if net_refdes & crystal_refs and net_refdes & controller_refs:
                    inferred.add(net_name)
        if "debug_interface" in roles:
            for net in self.net_names:
                if net.startswith("SWD") or net.startswith("JTAG"):
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
        role_alias_map: dict[str, list[str]]
        if self.resolver_mode == "legacy":
            role_alias_map = {role: sorted(list(aliases)) for role, aliases in LEGACY_ROLE_ALIASES.items()}
        else:
            role_alias_map = {
                role: cfg.get("aliases", []) for role, cfg in self.role_config.get("roles", {}).items()
            }
        for role, aliases in role_alias_map.items():
            if any(alias in q_lower for alias in aliases):
                roles.add(role)
        role_candidates: dict[str, list[dict[str, float | str]]] = {}
        role_confidence: dict[str, str] = {}
        unresolved_roles: list[str] = []
        for role in sorted(roles):
            if self.resolver_mode == "legacy":
                refdes = self.refdes_by_role_legacy.get(role, [])
                matched_refdes.extend(refdes)
                if refdes:
                    role_candidates[role] = [{"refdes": ref, "score": 2.0} for ref in refdes[:3]]
                    role_confidence[role] = "medium"
                else:
                    unresolved_roles.append(role)
                    role_confidence[role] = "low"
            else:
                cfg = self.role_config.get("roles", {}).get(role, {})
                top_n = int(cfg.get("top_n", 4))
                candidates = self.role_catalog.get(role, [])[:top_n]
                role_candidates[role] = candidates
                if candidates:
                    best = float(candidates[0]["score"])
                    role_confidence[role] = self._score_to_band(best)
                    matched_refdes.extend(str(item["refdes"]) for item in candidates)
                else:
                    unresolved_roles.append(role)
                    role_confidence[role] = "low"
        matched_nets.extend(self._infer_nets_from_text(q_lower))
        matched_nets.extend(self._infer_nets_from_topology(roles, set(matched_refdes)))
        return {
            "resolver_mode": self.resolver_mode,
            "roles": sorted(roles),
            "refdes": sorted(set(matched_refdes)),
            "nets": sorted(set(matched_nets)),
            "symbols": sorted(symbols),
            "role_candidates": role_candidates,
            "role_confidence": role_confidence,
            "unresolved_roles": sorted(unresolved_roles),
        }


class HybridRetriever:
    def __init__(self, project_root: Path, resolver_mode: str = "config"):
        derived = project_root / "derived"
        self.nets = read_jsonl(derived / "dsn" / "nets.jsonl")
        self.pin_to_net = read_json(derived / "dsn" / "pin_to_net.json")
        self.net_graph = read_json(derived / "dsn" / "net_graph.json")
        self.refdes_to_part = read_json(derived / "bom" / "refdes_to_part.json")
        self.pdf_chunks = read_jsonl(derived / "pdf" / "pdf_chunks.jsonl")
        self.parser = QueryParser(
            self.refdes_to_part,
            self.nets,
            project_root=project_root,
            resolver_mode=resolver_mode,
        )
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
        if not allowed:
            role_candidates = entities.get("role_candidates", {})
            for role in entities.get("roles", []):
                for item in role_candidates.get(role, [])[:3]:
                    refdes = item.get("refdes")
                    if not refdes:
                        continue
                    meta = self.refdes_to_part.get(str(refdes), {})
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
        unresolved_roles = entities.get("unresolved_roles", [])
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
            if source_type == "datasheet" and not lex and not allowed_datasheets and not unresolved_roles:
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
        effective_top_k = top_k + 2 if unresolved_roles else top_k
        datasheet = [row for row in scored if row.source_type == "datasheet"][:effective_top_k]
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
