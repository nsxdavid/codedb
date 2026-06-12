# codedb ranking experiments — what was tried and where it failed

_Archived from the working tree of `release/0.2.5825` (2026-06-08). Full
synthesis in [research.md](research.md); the prioritized P0–P4 plan in
[todo.md](todo.md); raw bench/timing output in [scratch/](scratch/)._

## TL;DR

Goal: close the codedb-vs-engram ranking gap — codedb MRR ≈ 0.32 vs engram's
learned re-rank ≈ 0.57 on the git-derived bench (commit subject → query,
changed file → gold). **Four single-signal reranking tweaks were tried; all
flat-lined.** One signal — the negative-lexical file-frequency penalty, ported
from engram's *learned* weights — moved the metric and **shipped**
(`LexFreqPenalty` in [../../src/explore.zig](../../src/explore.zig), default-on
amp 0.8). The failures all teach the same lesson: **hand-tuned single signals
don't move this metric; the win is multi-signal _learned_ fusion.**

The bench (`engram codedb-report`) resolves ~0.04 MRR per query, so "flat"
means *no gold changed rank* — not noise, a genuine null result.

## What failed (and the proof it failed)

### 1. #550 query-specific call-graph distance — FLAT → dropped (PR #552 closed)
Weighted spreading activation over the call graph (HippoRAG / MemGraphRAG-style,
deterministic). **MRR 0.125 → 0.119** on the #550 corpus; buried golds ranked
identically. Graph distance is not in the IR-fault-localization signal set at
all — no prior evidence it should move the metric, and it didn't.

### 2. Global centrality in `searchContent` rerank — FLAT
Raw `1 + 0.15·log(1+c)` centrality multiplier on the rerank score.
**MRR 0.322 → 0.322.**

### 3. Normalized centrality (up to 2×) — FLAT
**MRR 0.322 → 0.322.** Centrality is a *weak* fault-localization signal
("+0.1"-class weight in the literature); it only contributes inside a learned
multi-signal fusion, never as a standalone reranker.

### 4. rVSM file-size prior (BugLocator, ICSE 2012) — FLAT [code retained, default-OFF]
Multiplier `1 + amp·tanh(k·(line_count/avg − 1))` on the rerank score, code
files only. **MRR 0.357 OFF == 0.357 ON** at amp 0.5 *and* 1.0; per-query ranks
**byte-identical** (diff empty).

Debug-proven the prior *fires*: instrumented `multiplier()` gives `index.zig`
(3260 lines) ×2.000 at amp 1.0. It still stays #11 for "daemon" because the ~10
files ranked above it are *also* large **and** more lexically relevant — a
uniform size multiplier lifts them in lockstep. **Size alone cannot
differentially surface a non-lexically-dominant gold** (only a semantic/
structural signal can, which is why engram's learned reranker reaches #1).

- Retained as **opt-in scaffolding** in
  [../../src/explore.zig](../../src/explore.zig) (`RvsmSizePrior`, enabled by
  `CODEDB_RVSM_SIZE_PRIOR`, tuned via `CODEDB_RVSM_AMP` / `CODEDB_RVSM_K`) — a
  P4 learned-fusion *feature*, not a standalone reranker. No effect unless the
  env flag is set.

## What worked (for contrast — this is why the failures are instructive)

**Negative-lexical file-frequency penalty** — `LexFreqPenalty`, engram's
`LEARNED_W[lexical] = −2` ported into codedb's in-process rerank: down-weight
files the query *saturates* (dispatcher / registry / re-export / changelog) so
the eponymous implementation file surfaces. **MRR 0.833 → 1.000** on the
swe-lite retrieval subset, zero regressions; shipped **default-on at amp 0.8**.

It worked precisely *because* it is a slice of engram's **learned** weighting,
not a fresh hand-tuned signal — confirming the thesis the four nulls imply.

## Why the failures flat-lined (the synthesis)

We tuned *weak* fault-localization signals (centrality), a *non-FL* signal
(graph distance), and a size prior that mathematically can't disambiguate a
non-lexically-dominant gold — while ignoring the levers the IR-fault-
localization literature proves move this exact metric (AmaLgam: +46% MAP over a
history-aware baseline):

- **rVSM size prior _inside_ a learned fusion** (not standalone)
- git **version history**: per-file fix-frequency + fix-recency — *untried,
  codedb's single biggest signal gap*
- **structured retrieval** (BLUiR: query vs class/method/identifier name fields)
- **learned fusion** over all of the above — which is exactly what engram is (0.57)

Next levers, ranked by leverage (see [todo.md](todo.md)): **P1** git
fix-frequency (would flag churn-heavy core files like `index.zig` directly) and
**P4** route `codedb_search` through engram's learned re-rank (or port the
policy into codedb).

## Infra gotchas (cost every future ranking experiment)

1. The bench measures the **CLI `searchContent` rerank** (`rerankSignalScore` /
   `rerankAndFinalize`), **not** `searchContentRanked` / BM25 — that path is
   MCP-multiword-only. Touching BM25 norm `b` is a dead end for this bench.
2. Per-query ranks are **state-dependent** (warm cli-daemon + word-index
   completeness). Pin with `CODEDB_NO_CLI_DAEMON=1` + `pkill codedb` between
   runs — the warm daemon caches its spawn-time env, so a stale daemon silently
   serves the wrong flag. Aggregate MRR is stable; only per-query repro needs
   the pin.

## Separate track (NOT a failure): swe-lite functional benchmark

The swe-lite validators + results under
[`../../bench/swe-lite/validators/`](../../bench/swe-lite/validators/) are a
*different* experiment — functional validation of codedb-as-a-tool against
codegraph / graphify / leanctx / baseline (does the patch built with each tool's
context actually pass the repo's tests). They live in `bench/` because they
extend a tracked suite + runner, not here. Noted so this archive indexes all
the uncommitted research.

## References

BugLocator/rVSM (Zhou 2012); BLUiR (Saha 2013); AmaLgam (Wang & Lo 2014); BLIA
/ MrVSM (2015); Ye-LR (2014), FLUCCS (Sohn & Yoo 2017), PRINCE (Kim 2019);
evolutionary-coupling / logical-coupling literature. Full table in
[research.md](research.md).
