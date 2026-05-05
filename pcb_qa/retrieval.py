from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re

from .utils import canonical_net_name, cosine_similarity, hash_embedding, read_json, read_jsonl, tokenize


INTENTS = (
    "system_function",
    "relationship_trace",
    "pin_validation",
    "net_trace",
    "anomaly_check",
)


@dataclass
class RetrievedEvidence:
    source_type: str
    source_id: str
    score: float
    payload: dict


@dataclass
class RetrievalResult:
    intent: str
    entities: dict[str, list[str]]
    net_evidence: list[RetrievedEvidence]
    component_evidence: list[RetrievedEvidence]
    block_evidence: list[RetrievedEvidence]
    anomaly_evidence: list[RetrievedEvidence]
    datasheet_evidence: list[RetrievedEvidence]
    schematic_evidence: list[RetrievedEvidence]
    open_uncertainties: list[str]


def _extract_symbol_like(question: str) -> set[str]:
    candidates = set()
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.\\/-]*", question):
        if len(token) >= 2 and re.search(r"[A-Za-z]", token):
            candidates.add(token)
    return candidates


class IntentRouter:
    def classify(self, question: str) -> str:
        q = question.lower()
        if any(token in q for token in ("floating", "misconnect", "wrong connection", "short", "disconnect")):
            return "anomaly_check"
        if any(token in q for token in ("pin", "vdd", "vddio", "connected correctly", "is connected")):
            return "pin_validation"
        if any(token in q for token in ("net", "which pins are on", "trace", "path")):
            return "net_trace"
        if any(token in q for token in ("communicate", "interface", "between", "how does")):
            return "relationship_trace"
        return "system_function"


class QueryParser:
    def __init__(
        self,
        refdes_to_part: dict[str, dict],
        nets: list[dict],
        project_root: Path,
    ):
        _ = project_root
        self.refdes_to_part = refdes_to_part
        self.nets = nets
        self.known_refdes = {key.upper() for key in refdes_to_part}
        self.net_aliases = {
            canonical_net_name(alias)
            for net in nets
            for alias in net.get("aliases", []) + [net.get("net_name_raw", ""), net.get("net_name_canonical", "")]
        }
        self.router = IntentRouter()

    def parse(self, question: str) -> dict[str, list[str] | str]:
        symbols = _extract_symbol_like(question)
        refs = sorted({token.upper() for token in symbols if token.upper() in self.known_refdes})
        nets = sorted({canonical_net_name(token) for token in symbols if canonical_net_name(token) in self.net_aliases})
        return {
            "intent": self.router.classify(question),
            "refdes": refs,
            "nets": nets,
            "symbols": sorted(symbols),
        }


class SingleModeRetriever:
    def __init__(self, project_root: Path):
        derived = project_root / "derived"
        self.nets = read_jsonl(derived / "dsn" / "nets.jsonl")
        self.net_lookup = {row["net_name_canonical"]: row for row in self.nets}
        self.pin_to_net = read_json(derived / "dsn" / "pin_to_net.json")
        self.component_pin_index = read_json(derived / "dsn" / "component_pin_index.json")
        self.refdes_to_part = read_json(derived / "bom" / "refdes_to_part.json")
        self.pdf_chunks = read_jsonl(derived / "pdf" / "pdf_chunks.jsonl")
        self.function_blocks = read_json(derived / "kg" / "function_blocks.json").get("blocks", [])
        self.power_domains = read_json(derived / "kg" / "power_domains.json").get("domains", [])
        self.interface_buses = read_json(derived / "kg" / "interface_buses.json").get("buses", [])
        self.anomalies = read_jsonl(derived / "qa" / "connectivity_anomalies.jsonl")
        self.parser = QueryParser(self.refdes_to_part, self.nets, project_root=project_root)
        self.chunk_vecs = [hash_embedding(chunk.get("tokens", []), dims=512) for chunk in self.pdf_chunks]

    def _nets_for_refdes(self, refdes: str) -> set[str]:
        needle = f"{refdes.upper()}-"
        out: set[str] = set()
        for row in self.nets:
            for token in row.get("pins_raw", []):
                if token.upper().startswith(needle):
                    out.add(row["net_name_canonical"])
                    break
        return out

    def _component_evidence(self, entities: dict[str, list[str] | str]) -> list[RetrievedEvidence]:
        rows: list[RetrievedEvidence] = []
        for refdes in entities.get("refdes", []):
            meta = self.refdes_to_part.get(str(refdes), {})
            if not meta:
                continue
            rows.append(
                RetrievedEvidence(
                    source_type="component",
                    source_id=str(refdes),
                    score=2.0,
                    payload={"refdes": refdes, "bom": meta},
                )
            )
        rows.sort(key=lambda row: row.score, reverse=True)
        return rows

    def _net_evidence(self, entities: dict[str, list[str] | str], breadth: bool) -> list[RetrievedEvidence]:
        seed_nets: set[str] = set(str(net) for net in entities.get("nets", []))
        for refdes in entities.get("refdes", []):
            seed_nets.update(self._nets_for_refdes(str(refdes)))
        if breadth:
            # Breadth-first profile for generic questions.
            for row in self.nets:
                net = row.get("net_name_canonical", "")
                if net == "GND":
                    continue
                if len(seed_nets) >= 14:
                    break
                if any(h in net for h in ("VBUS", "VBAT", "1V8", "2V8", "SWD", "RESET", "P0.", "P1.", "P2.")):
                    seed_nets.add(net)
        rows: list[RetrievedEvidence] = []
        for net in sorted(seed_nets):
            payload = self.net_lookup.get(net)
            if not payload:
                continue
            rows.append(
                RetrievedEvidence(
                    source_type="net",
                    source_id=net,
                    score=2.0 + min(1.5, 0.05 * len(payload.get("pins_raw", []))),
                    payload=payload,
                )
            )
        rows.sort(key=lambda row: row.score, reverse=True)
        return rows

    def _block_evidence(self, entities: dict[str, list[str] | str], breadth: bool) -> list[RetrievedEvidence]:
        matched_refs = {str(item).upper() for item in entities.get("refdes", [])}
        matched_nets = {str(item).upper() for item in entities.get("nets", [])}
        rows: list[RetrievedEvidence] = []
        for block in self.function_blocks:
            refs = {str(ref).upper() for ref in block.get("component_refs", [])}
            nets = {str(net).upper() for net in block.get("nets", [])}
            overlap = len(matched_refs & refs) + len(matched_nets & nets)
            if overlap == 0 and not breadth:
                continue
            score = 1.0 + overlap
            if breadth:
                score += 0.6
            rows.append(
                RetrievedEvidence(
                    source_type="function_block",
                    source_id=str(block.get("block_id", "")),
                    score=score,
                    payload=block,
                )
            )
        rows.sort(key=lambda row: row.score, reverse=True)
        return rows[:6]

    def _anomaly_evidence(self, entities: dict[str, list[str] | str], aggressive: bool) -> list[RetrievedEvidence]:
        refs = {str(item).upper() for item in entities.get("refdes", [])}
        nets = {str(item).upper() for item in entities.get("nets", [])}
        out: list[RetrievedEvidence] = []
        for row in self.anomalies:
            row_ref = str(row.get("refdes", "")).upper()
            row_net = str(row.get("net_name", "")).upper()
            match = row_ref in refs or row_net in nets
            if not match and not aggressive:
                continue
            score = 2.1 if match else 1.1
            out.append(
                RetrievedEvidence(
                    source_type="anomaly",
                    source_id=str(row.get("anomaly_id", "")),
                    score=score,
                    payload=row,
                )
            )
        out.sort(key=lambda item: item.score, reverse=True)
        return out[:12 if aggressive else 6]

    def _pdf_evidence(self, question: str, entities: dict[str, list[str] | str], top_k: int) -> tuple[list[RetrievedEvidence], list[RetrievedEvidence]]:
        q_tokens = tokenize(question)
        q_set = set(q_tokens)
        q_vec = hash_embedding(q_tokens, dims=512)
        allowed_datasheets = set()
        for refdes in entities.get("refdes", []):
            meta = self.refdes_to_part.get(str(refdes), {})
            for candidate in meta.get("datasheet_candidates", []):
                allowed_datasheets.add(candidate)
        scored: list[RetrievedEvidence] = []
        for idx, chunk in enumerate(self.pdf_chunks):
            source_type = chunk.get("source_type", "")
            if source_type == "datasheet" and allowed_datasheets and chunk.get("source_file") not in allowed_datasheets:
                continue
            counts = Counter(chunk.get("tokens", []))
            lex = float(sum(counts.get(token, 0) for token in q_set))
            sem = cosine_similarity(q_vec, self.chunk_vecs[idx])
            score = lex + (0.35 * sem)
            if score <= 0.05:
                continue
            scored.append(
                RetrievedEvidence(
                    source_type=source_type,
                    source_id=str(chunk.get("chunk_id", f"chunk:{idx}")),
                    score=score,
                    payload=chunk,
                )
            )
        scored.sort(key=lambda item: item.score, reverse=True)
        datasheet = [row for row in scored if row.source_type == "datasheet"][:top_k]
        schematic = [row for row in scored if row.source_type == "schematic"][: max(3, top_k // 2)]
        return datasheet, schematic

    def retrieve(self, question: str, top_k: int = 8) -> RetrievalResult:
        parsed = self.parser.parse(question)
        entities = {
            "refdes": [str(item) for item in parsed.get("refdes", [])],
            "nets": [str(item) for item in parsed.get("nets", [])],
            "symbols": [str(item) for item in parsed.get("symbols", [])],
        }
        intent = str(parsed.get("intent", "system_function"))
        breadth = intent in {"system_function", "relationship_trace"}
        aggressive_anomaly = intent in {"anomaly_check", "pin_validation"}
        net_evidence = self._net_evidence(entities, breadth=breadth)
        component_evidence = self._component_evidence(entities)
        block_evidence = self._block_evidence(entities, breadth=breadth)
        anomaly_evidence = self._anomaly_evidence(entities, aggressive=aggressive_anomaly)
        datasheet_evidence, schematic_evidence = self._pdf_evidence(question, entities, top_k=top_k)
        open_uncertainties: list[str] = []
        if intent == "system_function" and len(net_evidence) < 5:
            open_uncertainties.append("insufficient_net_diversity_for_system_function")
        if aggressive_anomaly and not anomaly_evidence:
            open_uncertainties.append("no_matching_connectivity_anomalies_detected")
        return RetrievalResult(
            intent=intent,
            entities=entities,
            net_evidence=net_evidence,
            component_evidence=component_evidence,
            block_evidence=block_evidence,
            anomaly_evidence=anomaly_evidence,
            datasheet_evidence=datasheet_evidence,
            schematic_evidence=schematic_evidence,
            open_uncertainties=open_uncertainties,
        )


# Preserve import compatibility for existing call sites.
HybridRetriever = SingleModeRetriever
