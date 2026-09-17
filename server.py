"""
OCTAD 2.0 MCP Server
=====================
Exposes the OCTAD 2.0 disease signature / drug repurposing platform as MCP
tools for Claude. This is a thin translation layer — all real logic lives in
octad_core.R (shared with the Shiny portal) via the plumber API running on
127.0.0.1:8200.

No authentication (matches the InsilicoCell pattern): a public, unauthenticated
Streamable HTTP endpoint, gated by usage limits rather than credentials.
Discovery tools (get_octad_capabilities / get_octad_resource_limits) let
Claude explain what's available and what the limits are before running
anything expensive.

Run:  python server.py
Serves Streamable HTTP at http://127.0.0.1:8100/mcp (nginx proxies this
publicly at https://apps.octad.org/octad2/mcp).
"""

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

API_BASE = "http://127.0.0.1:8200"
TIMEOUT = httpx.Timeout(30.0, read=30.0)  # sync endpoints are fast; async job
                                            # status polls are lightweight too —
                                            # the actual RGES/indication compute
                                            # happens in the API's own background
                                            # future() workers, not here.

mcp = FastMCP(
    "OCTAD 2.0",
    host="127.0.0.1",
    port=8100,
    # nginx forwards the real client Host header ("apps.octad.org") through
    # to this server, which by default only trusts localhost — allowlist the
    # actual public domain instead of disabling DNS-rebinding protection.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["apps.octad.org", "apps.octad.org:*", "127.0.0.1:8100", "localhost:8100"],
        allowed_origins=["https://apps.octad.org"],
    ),
)


async def _get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(f"{API_BASE}{path}", params=params or {})
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, data: dict) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(f"{API_BASE}{path}", data=data)
        resp.raise_for_status()
        return resp.json()


# =============================================================================
# Discovery
# =============================================================================

@mcp.tool()
async def get_octad_capabilities() -> dict:
    """Get an overview of the OCTAD 2.0 platform: what modules exist, how many
    diseases/signatures/drugs are in the database, and what each tool does.
    Call this first if you're not sure what's available."""
    return await _get("/capabilities")


@mcp.tool()
async def get_octad_resource_limits() -> dict:
    """Get usage limits for this hosted, unauthenticated OCTAD 2.0 API —
    max gene list size, concurrent jobs, and expected runtimes for the async
    tools (RGES scoring, indication screening)."""
    return await _get("/resource_limits")


# =============================================================================
# Fast, synchronous tools
# =============================================================================

@mcp.tool()
async def list_diseases() -> dict:
    """List all diseases in the OCTAD 2.0 database (350 diseases, each with
    one or more RNA-seq datasets). Returns exact disease name strings to use
    in other tool calls."""
    return await _get("/diseases")


@mcp.tool()
async def list_tissues() -> dict:
    """List all tissue/organ labels available for tissue-restricted indication
    screening. Use "all" (the default) to search across every tissue."""
    return await _get("/tissues")


@mcp.tool()
async def search_gene(
    gene: str,
    mode: str = "disease",
    sig_only: bool = True,
    padj_cutoff: float = 0.05,
    logfc_cutoff: float = 0.5,
) -> dict:
    """Search for a gene (or comma-separated genes) across all 350 disease
    signatures in OCTAD 2.0. mode="disease" (default) averages multiple
    datasets per disease into one row; mode="dataset" returns one row per
    dataset. Returns which diseases the gene is significantly up/down-
    regulated in."""
    return await _get("/gene_search", {
        "gene": gene, "mode": mode, "sig_only": str(sig_only).lower(),
        "padj_cutoff": padj_cutoff, "logfc_cutoff": logfc_cutoff,
    })


@mcp.tool()
async def get_disease_signature(
    disease: str,
    use_meta: bool = True,
    signature_file: str | None = None,
    top_n: int = 25,
) -> dict:
    """Get the top up/down-regulated genes for one disease. use_meta=True
    (default) uses the random-effects meta-analyzed consensus signature across
    all of that disease's datasets; set use_meta=False and provide
    signature_file (an exact filename from a prior call) to use one specific
    dataset instead."""
    return await _get("/disease_signature", {
        "disease": disease, "use_meta": str(use_meta).lower(),
        "signature_file": signature_file, "top_n": top_n,
    })


@mcp.tool()
async def pathway_enrichment(
    disease: str,
    mode: str = "disease",
    signature_file: str | None = None,
    direction: str = "up",
    database: str = "GO_Biological_Process_2023",
    padj_cutoff: float = 0.05,
    logfc_cutoff: float = 1.0,
) -> dict:
    """Run pathway enrichment (GO, KEGG, Reactome, WikiPathways, or MSigDB
    Hallmark) on a disease's up- or down-regulated genes. mode="disease"
    (default) uses genes significant in >=2 of that disease's datasets;
    mode="dataset" requires a specific signature_file."""
    return await _get("/pathway_enrichment", {
        "disease": disease, "mode": mode, "signature_file": signature_file,
        "direction": direction, "database": database,
        "padj_cutoff": padj_cutoff, "logfc_cutoff": logfc_cutoff,
    })


@mcp.tool()
async def export_gps_signature(
    disease: str,
    use_meta: bool = True,
    signature_file: str | None = None,
) -> dict:
    """Export a disease signature in the two-column format (GeneSymbol, Value)
    expected by the GPS de novo compound-screening platform
    (https://apps.octad.org/GPS/)."""
    return await _get("/gps_export", {
        "disease": disease, "use_meta": str(use_meta).lower(),
        "signature_file": signature_file,
    })


# =============================================================================
# Async tools: Drug Repurposing (RGES)
# =============================================================================

@mcp.tool()
async def start_rges_job(
    disease: str,
    mode: str = "meta",
    signature_file: str | None = None,
    padj_cutoff: float = 0.05,
    logfc_cutoff: float = 0.5,
    max_genes: int = 100,
    permutations: int = 10000,
    max_i2: float = 100.0,
    min_k: int = 1,
) -> dict:
    """Start a drug repurposing (RGES) scoring job — screens the LINCS
    compound library (~12,400 compounds) for their ability to reverse a
    disease's transcriptional signature. Takes 1-3 minutes, so this returns
    immediately with a job_id; poll get_rges_job_status(job_id) until
    status="done". mode="meta" (default) uses the meta-analyzed consensus
    signature; mode="dataset" requires signature_file. Negative sRGES =
    stronger reversal = better repurposing candidate."""
    data = {"disease": disease, "mode": mode, "padj_cutoff": padj_cutoff,
            "logfc_cutoff": logfc_cutoff, "max_genes": max_genes,
            "permutations": permutations, "max_i2": max_i2, "min_k": min_k}
    if signature_file:
        data["signature_file"] = signature_file
    return await _post("/rges/start", data)


@mcp.tool()
async def get_rges_job_status(job_id: str, fda_only: bool = False, top_n: int = 25) -> dict:
    """Poll the status of an RGES scoring job started with start_rges_job.
    Set fda_only=True to restrict results to FDA-approved/launched/clinical-
    phase compounds only (most of the ~12,400 LINCS library are preclinical
    research compounds with no drug name — fda_only filters to the ones with
    an actual clinical status). Returns status="running", "done" (with the
    top-ranked compounds by sRGES, including drug name, target, MoA, and
    clinical phase where known), or "error" (with a message explaining why)."""
    return await _get(f"/rges/status/{job_id}", {
        "fda_only": str(fda_only).lower(), "top_n": top_n,
    })


# =============================================================================
# Async tools: Indication Expansion
# =============================================================================

@mcp.tool()
async def start_indication_job(
    up_genes: str = "",
    down_genes: str = "",
    id_type: str = "symbol",
    tissue: str = "all",
    padj_cutoff: float = 0.05,
    logfc_cutoff: float = 1.0,
    max_genes: int = 500,
    p_cutoff: float = 0.05,
) -> dict:
    """Start an indication-expansion screen — given a drug's own gene
    signature (comma or newline-separated up/down gene lists), screens every
    disease-tissue dataset in OCTAD 2.0 and classifies each as a significant
    reversal ("Yes"), significant concordance ("No" — may worsen), or no
    significant association ("Neutral"), via Fisher's-combined hypergeometric
    enrichment testing. At least one of up_genes/down_genes is required.
    Takes 1-2 minutes; returns immediately with a job_id to poll with
    get_indication_job_status."""
    return await _post("/indication/start", {
        "up_genes": up_genes, "down_genes": down_genes, "id_type": id_type,
        "tissue": tissue, "padj_cutoff": padj_cutoff,
        "logfc_cutoff": logfc_cutoff, "max_genes": max_genes,
        "p_cutoff": p_cutoff,
    })


@mcp.tool()
async def get_indication_job_status(job_id: str) -> dict:
    """Poll the status of an indication-expansion job started with
    start_indication_job. Returns status="running", "done" (with the top 25
    disease-tissue matches ranked by significance), or "error"."""
    return await _get(f"/indication/status/{job_id}")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
