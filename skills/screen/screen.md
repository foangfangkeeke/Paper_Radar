# Screen

Two-stage screening for the Paper Push Agent: **Preliminary** (abstract → score + tags + comment → enqueue) and **Detailed** (full-text → refine tags + comment).

## Scripts

- **`find_candidates.py`** — Find paper_items with abstracts not yet in base_queue (saves Paper Push Agent tokens)
- **`rank_push_queue.py`** — List papers in push_queue with Tags/Comment, supports `--unpushed`, `--limit`, `--offset` for pagination

## 1. Preliminary Screen (Paper Push Agent)

1. Run `find_candidates.py` to get candidate list
2. For each paper, read abstract → score 4 dimensions independently (1-10 each):
   - **DirectionRelevance** — topic match with research direction
   - **MethodRelevance** — method relevance
   - **Novelty** — method/approach novelty
   - **Transferability** — cross-domain applicability
3. **Score** = floor(average of 4 dimensions). 1-10 scale.
4. Generate Tags + Comment (~100-200 chars: problem + method + value)
5. Write **all scored papers** to `paper_base_queue.json`, plus `paper_push_queue.json` if Score >= 8

## 2. Detailed Screen (Paper Push Agent)

1. Triggered after PDF download, before Zotero import
2. Extract full text via `pdftotext`
3. Produce refined Tags + Comment (4-part: Research problem / Key assumptions / Method / Key findings, ~500-1000 chars)
4. **Do NOT write back to queues** — pass results to zotero_import.py via `--tags-json` and `--comment`

## 3. Push & Dynamic Scoring

### Traversal Strategy

- Use `rank_push_queue.py --unpushed --json --limit N --offset M` to fetch a batch of candidates (default N=10, M=0)
- Each candidate includes Tags and Comment for informed matching
- If no match in the first batch, increase offset and fetch the next batch
- Push one paper at a time; stop at the first qualifying match
- No pre-sorting — the Agent scans in queue order

### Two Qualifying Match Scenarios

1. **User specifies a preference** (e.g., "Benders decomposition"): match keywords in Title / Tags / Comment / Keywords / Method; push on match
2. **User gives no preference** (e.g., just says "push"): if dynamic criteria are empty, push the first unpushed paper from the top; otherwise apply dynamic scoring and push the first paper with dynamic score >= 0

### Dynamic Scoring

`Score` is used for preliminary queue eligibility. Papers enter `paper_push_queue.json` according to the screening rule above.

During push selection, the Paper Push Agent applies user preference and optional dynamic rules only within the already-filtered `paper_push_queue`.

Dynamic scoring is optional.

If dynamic preference rules are empty:
- do not invent direction or method bonuses;
- do not create hidden preferences;
- push the first unpushed paper according to the existing queue order, unless the user gives a specific preference.

If dynamic preference rules are defined:

```text
DynamicScore = DirectionBonus + MethodBonus + FeedbackAdjustment

### Push Display Format

Shown directly to user in conversation; do NOT create .md files:

```
Total: 1

## 1. Paper Title
- Journal: xxx
- Score: x (+y dynamic)
- Key: doi:xxx
- Download: OK (path) or FAILED
- Comment: one-sentence summary
- Tags: key tags (semicolon-separated)
- Abstract: full abstract
```

## 4. Tags Specification

Format: `field: value`. Only include fields that apply (never `field: none`). Target 10-15 tags.

Tags should be specific and descriptive, not generic. Compare:
- Bad: `Method: optimization`
- Good: `Method: risk-averse two-stage stochastic programming with CVaR`

Each tag value should convey enough detail that reading tags alone gives a clear picture of the paper's approach.

### Common Fields

| Field | Meaning |
|-------|---------|
| `Object` | System/entity studied |
| `Application domain` | Application area |
| `Method` | Core methodology |
| `Decision level` | tactical / operational / strategic |
| `Objective` | Optimization goal |
| `Evaluation setting` | Real data, case study location, scale |
| `Transferable idea` | One sentence — what can be reused |
| `Keywords` | Technical keywords (list) |

### Direction-specific Fields

**`transport_energy_integration`**: `Transport mode`, `Energy component`, `Infrastructure`, `Charging type`, `Grid interaction`

**`transport_emergency_resilience`**: `Transport mode`, `Disruption type`, `Resilience aspect`, `Response strategy`, `Uncertainty`

**`or_ai`**: `Problem type`, `Solver/algorithm`, `Decomposition`, `AI component`, `OR component`

**`ai_behavior`**: `Transport mode`, `Behavior subject`, `Behavior model`, `Heterogeneity`, `Prediction target`

## 5. Comment Specification

### Preliminary Comment
One paragraph, ~100-200 chars: **Problem** (context + challenge) + **Method** (core approach) + **Value** (key result).

### Detailed Comment
~500-1000 chars, four parts:
```
Research problem: <what problem, why it matters, what gap it fills>
Key assumptions: <critical modeling assumptions>
Method: <technical approach, key innovations>
Key findings: <main results, quantitative if available>
```

## Execution

Both screening stages are executed conversationally by the Paper Push Agent. Python scripts handle candidate filtering and queue listing only. Do not edit source code, README, developer docs, or repository structure while acting as Paper Push Agent.
