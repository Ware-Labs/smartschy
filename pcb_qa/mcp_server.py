from pathlib import Path
from typing import Any

from . import evidence_tools
from .evidence_packet import build_evidence_packet as build_packet_func
from .prompt_render import render_prompt_from_evidence_packet as render_prompt_func


def _to_path(project_root: str) -> Path:
    return Path(project_root).resolve()


def build_mcp_server() -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as exc:  # pragma: no cover - import guard
        raise RuntimeError(
            "MCP server dependencies are missing. Install the 'mcp' package first."
        ) from exc

    mcp = FastMCP("pcb-qa-evidence")

    @mcp.tool()
    def list_project_summary(project_root: str) -> dict:
        return evidence_tools.list_project_summary(_to_path(project_root))

    @mcp.tool()
    def search_components(project_root: str, query: str, component_type: str | None = None) -> dict:
        return evidence_tools.search_components(
            _to_path(project_root), query=query, component_type=component_type
        )

    @mcp.tool()
    def get_component(project_root: str, refdes: str) -> dict:
        return evidence_tools.get_component(_to_path(project_root), refdes=refdes)

    @mcp.tool()
    def get_component_pins(project_root: str, refdes: str) -> dict:
        return evidence_tools.get_component_pins(_to_path(project_root), refdes=refdes)

    @mcp.tool()
    def get_pin_net(project_root: str, refdes: str, pin: str) -> dict:
        return evidence_tools.get_pin_net(_to_path(project_root), refdes=refdes, pin=pin)

    @mcp.tool()
    def search_nets(project_root: str, query: str) -> dict:
        return evidence_tools.search_nets(_to_path(project_root), query=query)

    @mcp.tool()
    def get_net(project_root: str, net_name_or_alias: str) -> dict:
        return evidence_tools.get_net(
            _to_path(project_root), net_name_or_alias=net_name_or_alias
        )

    @mcp.tool()
    def get_net_members(project_root: str, net_name: str) -> dict:
        return evidence_tools.get_net_members(_to_path(project_root), net_name=net_name)

    @mcp.tool()
    def trace_net_neighborhood(
        project_root: str,
        seed_nets: list[str] | None = None,
        seed_pins: list[str] | None = None,
        seed_components: list[str] | None = None,
        depth: int = 1,
        max_nodes: int = 250,
    ) -> dict:
        return evidence_tools.trace_net_neighborhood(
            _to_path(project_root),
            seed_nets=seed_nets or [],
            seed_pins=seed_pins or [],
            seed_components=seed_components or [],
            depth=depth,
            max_nodes=max_nodes,
        )

    @mcp.tool()
    def search_pdf_chunks(
        project_root: str,
        query: str,
        source_type: str = "any",
        refdes: str | None = None,
        mpn: str | None = None,
        max_results: int = 10,
    ) -> dict:
        return evidence_tools.search_pdf_chunks(
            _to_path(project_root),
            query=query,
            source_type=source_type,
            refdes=refdes,
            mpn=mpn,
            max_results=max_results,
        )

    @mcp.tool()
    def get_pdf_chunk(project_root: str, chunk_id: str) -> dict:
        return evidence_tools.get_pdf_chunk(_to_path(project_root), chunk_id=chunk_id)

    @mcp.tool()
    def find_datasheets_for_component(project_root: str, refdes: str) -> dict:
        return evidence_tools.find_datasheets_for_component(
            _to_path(project_root), refdes=refdes
        )

    @mcp.tool()
    def search_datasheet_chunks(project_root: str, refdes: str, query: str, max_results: int = 8) -> dict:
        return evidence_tools.search_datasheet_chunks(
            _to_path(project_root), refdes=refdes, query=query, max_results=max_results
        )

    @mcp.tool()
    def get_schematic_pages(
        project_root: str,
        query: str | None = None,
        refdes: str | None = None,
        net: str | None = None,
        max_results: int = 6,
    ) -> dict:
        return evidence_tools.get_schematic_pages(
            _to_path(project_root),
            query=query,
            refdes=refdes,
            net=net,
            max_results=max_results,
        )

    @mcp.tool()
    def get_schematic_page_image(project_root: str, page_number: int, include_bytes: bool = False) -> dict:
        payload = evidence_tools.get_schematic_page_image(
            _to_path(project_root),
            page_number=page_number,
            include_bytes=include_bytes,
        )
        if include_bytes and "image_bytes" in payload:
            payload["image_bytes"] = payload["image_bytes"].hex()
            payload["image_encoding"] = "hex"
        return payload

    @mcp.tool()
    def get_component_context_bundle(
        project_root: str, refdes: str, query: str | None = None, max_results: int = 6
    ) -> dict:
        return evidence_tools.get_component_context_bundle(
            _to_path(project_root), refdes=refdes, query=query, max_results=max_results
        )

    @mcp.tool()
    def build_evidence_packet(
        project_root: str,
        question: str,
        selected_evidence: list[dict],
        agent_trace: dict | None = None,
        resolved_entities: dict | None = None,
        open_uncertainties: list[str] | None = None,
        recommended_answer_constraints: list[str] | None = None,
        limits: dict | None = None,
        stop_reason: str | None = None,
    ) -> dict:
        return build_packet_func(
            project_root=_to_path(project_root),
            question=question,
            selected_evidence=selected_evidence,
            agent_trace=agent_trace,
            resolved_entities=resolved_entities,
            open_uncertainties=open_uncertainties,
            recommended_answer_constraints=recommended_answer_constraints,
            limits=limits,
            stop_reason=stop_reason,
        )

    @mcp.tool()
    def render_prompt_from_evidence_packet(project_root: str, evidence_packet: dict) -> dict:
        _ = _to_path(project_root)
        prompt = render_prompt_func(evidence_packet)
        return {"prompt_text": prompt}

    return mcp


def main() -> int:
    server = build_mcp_server()
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
