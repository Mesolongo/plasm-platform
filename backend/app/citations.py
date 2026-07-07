"""Literature-grounded hypothesis citations (Phase 3).

For each hypothesized path (from_construct -> to_construct) we query Crossref for
peer-reviewed work that plausibly grounds the relationship, and return a short list
of candidate references (title, authors, year, DOI). Crossref is free and needs no
key; we identify ourselves in the User-Agent to use its "polite pool".

These are *suggestions* for a literature review, not automatic claims of support —
the researcher decides what actually cites. Full-name query terms come from the
construct labels the researcher chose (verbatim if they are already descriptive).
"""
import httpx

CROSSREF_URL = "https://api.crossref.org/works"
USER_AGENT = "plsem-platform/0.3 (research tool; mailto:support@plsem.local)"
DEFAULT_ROWS = 3


def _format_authors(authors: list[dict], limit: int = 3) -> str:
    names = []
    for a in authors[:limit]:
        family = a.get("family") or a.get("name") or ""
        if family:
            names.append(family)
    label = ", ".join(names)
    if len(authors) > limit:
        label += " et al."
    return label


def _parse_work(item: dict) -> dict:
    title = (item.get("title") or [""])[0]
    issued = item.get("issued", {}).get("date-parts", [[None]])
    year = issued[0][0] if issued and issued[0] else None
    doi = item.get("DOI")
    return {
        "title": title,
        "authors": _format_authors(item.get("author") or []),
        "year": year,
        "venue": (item.get("container-title") or [""])[0],
        "doi": doi,
        "url": f"https://doi.org/{doi}" if doi else None,
        "cited_by": item.get("is-referenced-by-count"),
    }


def _query_for(from_c: str, to_c: str) -> str:
    return f"{from_c} {to_c} structural equation modeling"


def search_crossref(query: str, rows: int = DEFAULT_ROWS,
                    client: httpx.Client | None = None) -> list[dict]:
    """Query Crossref for `query`, most-cited first; returns parsed references."""
    params = {
        "query.bibliographic": query,
        "rows": rows,
        "select": "DOI,title,author,issued,container-title,is-referenced-by-count",
        "sort": "relevance",
    }
    owns = client is None
    client = client or httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT})
    try:
        resp = client.get(CROSSREF_URL, params=params)
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
    finally:
        if owns:
            client.close()
    return [_parse_work(it) for it in items]


def suggest_for_hypotheses(hypotheses: list[dict],
                           rows: int = DEFAULT_ROWS) -> list[dict]:
    """For each hypothesis, attach candidate grounding references from Crossref.

    Direct paths only — a moderation (interaction) term is not a literature claim in
    its own right, so we skip those rather than search on a synthetic construct name.
    """
    out = []
    with httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
        for h in hypotheses:
            if h.get("type") == "moderation":
                continue
            parts = [p.strip() for p in h["path"].split("->")]
            if len(parts) != 2:
                continue
            from_c, to_c = parts
            try:
                refs = search_crossref(_query_for(from_c, to_c), rows, client=client)
                error = None
            except Exception as exc:  # one bad lookup should not sink the batch
                refs, error = [], str(exc)
            out.append({
                "hypothesis": h.get("hypothesis"),
                "path": h["path"],
                "query": _query_for(from_c, to_c),
                "references": refs,
                "error": error,
            })
    return out
