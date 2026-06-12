# codedb retrieval-ranking — research & synthesis

_Working notes for #546 (ranking quality) / #550 (query-specific signals). Captured 2026-06-07. Not yet committed; scratch/planning doc._

## The problem
codedb's lexical search (BM25/trigram + word index) under-ranks the file a query actually targets. Measured by `engram codedb-report` (git-derived queries: commit subject → query, changed file → gold): **codedb MRR ≈ 0.32 vs engram (learned re-rank) ≈ 0.57** on the codedb repo. ~7/24 queries are *recall* gaps (gold not retrieved at all); the rest are *buried* (gold retrieved but ranked #16–17).

## What we tried — and the negative results
All measured against `engram codedb-report` (keep-if-it-moves):
- **#550 query-specific call-graph distance** (weighted spreading activation, HippoRAG/MemGraphRAG-style, deterministic): MRR flat (0.125→0.119 on the #550 corpus; buried golds rank identically). **Dropped** — PR #552 closed.
- **Global centrality in `searchContent` rerank** (raw `1+0.15·log(1+c)`): flat (0.322→0.322).
- **Normalized centrality** (up to 2×): flat (0.322→0.322).

Three reranking tweaks → **zero metric movement.** The bench detects a single gold changing rank (~0.04 MRR), so flat = *no gold moved.* The lever is not in the signals we tried.

## The reframe: this IS IR-based Fault Localization (IRFL)
"Given a query (bug report / commit subject), rank source files by likelihood of being the relevant/changed one" is **exactly IRFL** — a mature, **embedding-free** field whose benchmark gold is the changed file (same as our git-bench). Critically, IRFL methods **move that metric** (AmaLgam: **+46% MAP** over a history-aware baseline, +24% over BugLocator) — using signals codedb never tried.

### Canonical methods + their signals
| Method | Signals it adds | codedb status |
|---|---|---|
| **BugLocator** (rVSM, 2012) | lexical VSM **× file-size prior** `g(#words)=1/(1+e⁻ˣ)` + similar-bug | **BACKWARDS** — codedb's BM25 length-norm *penalizes* big files |
| **BLUiR** (2013) | **structured** IR: query vs class/method/identifier *fields* | weak (`+5` symbol-def boost only) |
| **AmaLgam** (2014) | **version history + similar-report + structure**, fused | ❌ no history |
| **Rahman/MrVSM, BLIA** (2015) | **fix-frequency + fix-recency** (git history) | ❌ git-HEAD only; mtime is codedb's only temporal axis |
| **FLUCCS / PRINCE / Ye-LR** | **learning-to-rank** over sparse features: text-sim, fix-freq/recency, **fan-in/out (centrality)**, size, complexity | **= engram** (the 0.57 re-ranker); LTR > hand-tuned |
| evolutionary coupling | git **co-change** `A⇒B` (support + confidence) | ❌ |

### Why our experiments flat-lined (now obvious)
We tuned **centrality** (a *weak* FL signal — "+0.1" weight) and **graph-distance** (not in the FL signal set at all), while ignoring the levers the literature shows actually move the metric: the **rVSM size prior** (codedb does the opposite), **version history**, **structured retrieval**, and **learned fusion**.

## The synthesized algorithm: "codedb-FL"
An AmaLgam-style fusion mapped onto codedb's existing, embedding-free primitives:

```
Score(file, q) = fuse(
  lexical    : BM25/word(q, file) × g(#words)            # rVSM size prior  — BugLocator
  structured : boost if q matches a defined symbol name  # BLUiR (codedb symbol index)
  history    : fix_frequency(file) + fix_recency(file)   # AmaLgam / Rahman (git log)
  centrality : normalized call-graph fan-in/out          # PRINCE (codedb call graph)
)
```
**Fusion:** start with AmaLgam's weighted linear combo; ideally a **light learning-to-rank** trained on git history (commit→query, changed files→gold) — literally what engram does, and what FLUCCS/PRINCE validate (learned > hand-tuned, which is why our hand-tuning flat-lined).

## Key insights
1. **codedb's length normalization is backwards for this task.** BM25 penalizes long docs; rVSM (and our buried golds `explore.zig` / `main.zig`, which are big) say *favor* big core files. Cheapest high-leverage test.
2. **Version history is codedb's biggest gap** and IRFL's strongest external signal. Mining `git log` for per-file fix-freq/recency (+ co-change) is the #550 "git co-change" sub-item — now evidence-backed.
3. **Hand-tuned fusion under-performs learned fusion** (our 3 flat experiments; FLUCCS/PRINCE). engram already IS the learned re-ranker (0.57). The git-derived bench is the *right* fitness function — it's the IRFL metric, and IRFL proves it's movable.
4. **Recall (7/24 gaps) is orthogonal** — reranking can't fix files that aren't retrieved. Separate track (some gaps are git-label noise; some may be real #547-class search-blindness).

## P0 result (2026-06-07): rVSM size prior — FLAT (4th negative)

Implemented behind `CODEDB_RVSM_SIZE_PRIOR` (amp `CODEDB_RVSM_AMP`, slope `CODEDB_RVSM_K`):
a multiplier on the rerank score `1 + amp·tanh(k·(line_count/avg − 1))`, code files only,
applied in `rerankAndFinalize` — the path the bench actually exercises (see correction #1).

**Result** (`engram codedb-report ~/cdb-corpus-fl 30`, forced-cold to pin state):
codedb MRR = **0.357 OFF == 0.357 ON** at amp 0.5 *and* amp 1.0. OFF vs ON per-query ranks
are byte-identical (diff empty). The two non-MISS buried big-file golds never moved:
`daemon`→`index.zig` stayed #11, `navigation`→`main.zig` stayed #8.

**Why — debug-proven, not speculation.** Instrumented `multiplier()` confirms the prior runs in
the bench and computes the intended boost: `index.zig` (3260 lines) gets ×2.000 at amp 1.0. It
*still* stays #11 because the ~10 files ranked above it for "daemon" (main.zig, watcher, …)
contain *more* "daemon" text (higher base rerank score) AND are *also* large, so a uniform size
multiplier lifts them in lockstep. The git-gold (index.zig was *changed* in a daemon commit) is
not the lexically/size-dominant file — only a semantic/structural signal surfaces it, which is
exactly why engram's learned reranker reaches #1 (centrality/eponymy). **Size alone cannot
differentially surface a non-lexically-dominant gold.**

**Two infra corrections (matter for every future experiment):**
1. The bench measures the **CLI `searchContent` rerank** (`rerankSignalScore` /
   `rerankAndFinalize`), NOT `searchContentRanked`/BM25 — that path is MCP-multiword-only, and
   `renderPlainSearch` is MCP-only too. The todo's "touch searchContentRanked (BM25 norm b)" is a
   dead end for this bench.
2. Per-query ranks are **state-dependent** (warm cli-daemon + word-index completeness): an
   isolated `codedb search daemon` ranks `index.zig` #5 and the prior *does* move it #5→#1, but
   the bench's complete-index state ranks it #11, where the prior can't. Pin state with
   `CODEDB_NO_CLI_DAEMON=1` + a `pkill` between runs (the warm daemon caches its spawn-time env, so
   a stale daemon silently serves the wrong flag). Aggregate MRR is sound (OFF = 0.357 cold *and*
   warm); only per-query reproduction needs the pin.

**Takeaway:** 4/4 uniform single-signal tweaks now flat-line — confirms the thesis. `line_count`
is a clean, correct, always-available signal (verified) but inert as a standalone reranker; keep
it as a **feature for P4** (learned fusion can weight size *jointly* with the text/history signals
that disambiguate the gold). Best next lever: **P1** (git fix-frequency would flag `index.zig`
directly — it's a churn-heavy core file) or **P4** (route `codedb_search` through engram's learned
reranker, already at 0.585).

## References
- Zhou et al., *Where Should the Bugs Be Fixed?* (BugLocator / rVSM), ICSE 2012.
- Saha et al., *Improving Bug Localization using Structured IR* (BLUiR), ASE 2013.
- Wang & Lo, *Version History, Similar Report, and Structure* (AmaLgam), ICPC 2014.
- Youm et al., *BLIA*, 2015; Rahman et al. (MrVSM — fix-freq/recency, filename match), 2015.
- Ye et al. (*LR* learning-to-rank FL), 2014; Sohn & Yoo, *FLUCCS*, ISSTA 2017; Kim et al., *PRINCE*, TOSEM 2019.
- Evolutionary coupling / co-change: logical-coupling literature (support/confidence over git commits).
- (Agentic-memory adjacent, prior research: HippoRAG / MemGraphRAG — PPR over a KG; A-Mem; Mem0 v3 — relationships as a ranking boost, not a queryable graph.)
