---
name: research-preferences
description: Research direction priorities, method bonuses, and push rules for paper push
metadata:
  type: project
---

## Research Interests

- Transportation-energy integration (EV charging, smart grid, demand response)
- Transportation emergency resilience (transit disruption, service recovery)
- OR + AI (ML for optimization, decomposition methods, branch-and-bound)
- AI + travel behavior (discrete choice, mode choice modeling)

Keyword groups and target journals are defined in `skills/fetch-merge/paper-watch.config.json`.

## Journal Bonuses

High-impact OR/transportation journals receive substantial dynamic score boosts to prioritize them in the push queue.

Priority: **INFORMS > Transportation Research Part B > European Journal of Operational Research**

| Journal | Bonus | Notes |
|---|---|---|
| INFORMS Journal on Computing | +5 | INFORMS flagship computing journal |
| INFORMS Journal on Optimization | +5 | INFORMS optimization journal |
| Transportation Science | +5 | INFORMS journal |
| Operations Research | +5 | INFORMS flagship journal |
| Manufacturing & Service Operations Management | +5 | INFORMS journal |
| Any journal with "INFORMS" in the name | +5 | Catch-all for other INFORMS journals |
| Transportation Research Part B | +4 | Top methodological journal in transportation |
| European Journal of Operational Research | +3 | Premier European OR journal |

**Matching rule**: case-insensitive match on the Journal field. Match against the full journal name, not arbitrary substrings — avoid false matches (e.g., "Operations Research" should NOT match "Computers & Operations Research"). Use these specific patterns:

| Match pattern | Bonus |
|---|---|
| Journal contains "INFORMS" (any INFORMS-named journal) | +5 |
| Journal equals (ignoring case) "Transportation Science" | +5 |
| Journal equals (ignoring case) "Operations Research" | +5 |
| Journal equals (ignoring case) "Manufacturing & Service Operations Management" | +5 |
| Journal equals (ignoring case) "Mathematics of Operations Research" | +5 |
| Journal contains "Transportation Research Part B" | +4 |
| Journal contains "European Journal of Operational Research" | +3 |

Use `journal.strip().lower()` for comparison. For "equals" patterns, match the normalized journal name exactly. For "contains" patterns, use substring match on the normalized name. If a paper matches multiple patterns, use the highest single bonus (do not stack).

## Direction Priorities

| Direction | Push Bonus |
|---|---|
| (to be updated) | |

## Method Bonuses

| Method | Bonus |
|---|---|
| (to be updated) | | |

## Feedback Rules

User feedback (`data/feedback.json`) calibrates the above over time:
- Positive on a paper → increase bonus for similar methods/directions
- Negative → decrease bonus or raise qualifying threshold
- Patterns may introduce new criteria or retire stale ones

## Dynamic Scoring

During push selection, apply:

```text
DynamicScore = JournalBonus + DirectionBonus + MethodBonus + FeedbackAdjustment
```

- **JournalBonus**: applied first — case-insensitive substring match on Journal field against the Journal Bonuses table above
- **DirectionBonus**: matched from Direction Priorities table
- **MethodBonus**: matched from Method Bonuses table
- **FeedbackAdjustment**: from `data/feedback.json` calibration
- If a paper matches multiple journal patterns, use the highest single bonus (do not stack)

## Push Rule

Only papers already in `data/paper_push_queue.json` are eligible for push.

`Score` is used for preliminary queue eligibility. Papers enter `paper_push_queue.json` only if they satisfy the screening rule in `skills/screen/screen.md`.

During push selection, do not reuse base `Score` unless the user explicitly asks. Use dynamic preferences only within the already-filtered push queue.

If dynamic preference rules are empty:
- do not invent direction or method bonuses;
- do not create hidden preferences;
- push the first unpushed paper from the top of the queue, unless the user gives a specific preference.