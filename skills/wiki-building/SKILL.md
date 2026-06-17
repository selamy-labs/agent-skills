---
name: wiki-building
description: Use when building or maintaining a durable, compounding knowledge base queried by feeding markdown into a long-context model, with no RAG, chunking, or embeddings. Based on Andrej Karpathy's LLM-Wiki pattern.
---

# Wiki Building (the LLM-Wiki pattern)

A durable, **plain-markdown** knowledge base that **compounds** over time and is queried by feeding files into a **long-context model** — **no RAG, no chunking, no embeddings.** Based on Andrej Karpathy's LLM-Wiki pattern. This skill is **how to build and maintain** the wiki; pair it with wherever it lives (any read/write store).

## The structure
```
/raw     immutable source material — clips, transcripts, articles, logs.
         APPEND-ONLY; never edit. This is the ground truth.
/wiki     AI-"compiled" pages derived from /raw:
            • concept pages   (one idea, explained)
            • entity pages    (one person/system/thing)
            • synthesis pages (cross-cutting, connects concepts)
agents.md  operational spec: how an agent should read/maintain the wiki.
```

## The compiler analogy (the core mental model)
Treat knowledge like a build: **compile `/raw` → `/wiki` artifacts once; query the artifact, not the sources.** You don't re-derive understanding on every query — you compile it into legible pages and read those. When `/raw` grows, recompile the affected pages.

## How to query
**Feed the relevant `/wiki` (and `/raw` when needed) files into a long-context model.** No retrieval pipeline, no vector store, no chunking. The long context *is* the retrieval. This is simpler, fully legible (you can read every page), and avoids the failure modes of embedding-based RAG (chunk boundaries, stale indexes, opaque relevance).

## Maintenance rules
1. **`/raw` is append-only and immutable** — corrections happen in `/wiki`, not by editing source.
2. **`/wiki` pages are recompiled** from `/raw` as material accumulates — keep them synthesized, not just concatenated.
3. **One page = one concept/entity** (mirrors one-fact-per-file memory discipline); link pages liberally.
4. **`agents.md` defines the contract** — how agents read, when they recompile, how they add to `/raw`.
5. **Legibility over cleverness** — a human can read any page; that's the point.

## When to use / not
- **Use** for building a compounding, legible knowledge base queried by long context.
- **Not** for high-churn structured data needing real-time queries (use a database), or when the corpus genuinely exceeds practical context limits and retrieval is unavoidable.

## Anti-patterns
- Reaching for RAG/embeddings/chunking by default when long-context + compiled pages suffice.
- Editing `/raw` (destroys ground truth) instead of correcting in `/wiki`.
- Dumping concatenated sources into `/wiki` without synthesis (no compile step).
- Duplicate wikis with drifting content instead of one recompiled source.
