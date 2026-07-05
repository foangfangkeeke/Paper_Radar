# Paper Radar

Automated academic paper discovery, screening, PDF download, and Zotero delivery workflow.

This README is the project map only. Detailed development rules live in the Developer Agent and `docs/`. Detailed paper-push rules live in the Paper Push Agent and `skills/`.

## Agent Roles

Use different agents for different work. Do not mix these roles.

| Need | Use | Entry |
|---|---|---|
| Develop, fix bugs, review code, maintain docs/configs, verify workflow entry points | Developer Agent | [agents/developer-agent.md](agents/developer-agent.md) |
| Daily paper screening, ranking, push, PDF download, Zotero import, tags/comment generation | Paper Push Agent | [agents/paper-push-agent.md](agents/paper-push-agent.md) |

Developer Agent should not run subjective daily paper push by default. Paper Push Agent should not edit source code, README, developer docs, config templates, dependencies, or repository structure.

## Pipeline

```text
fetch-merge -> screen -> rank/push -> download -> detailed screen -> zotero import
```

| Stage | Primary Owner | Entry Point | Input | Output |
|---|---|---|---|---|
| Fetch + merge | Developer Agent / Python | [skills/fetch-merge/wos_cdp_workflow.py](skills/fetch-merge/wos_cdp_workflow.py), [skills/fetch-merge/merge_exports.py](skills/fetch-merge/merge_exports.py) | WoS queries/config | `data/paper_items.json` |
| Preliminary screen | Paper Push Agent | [skills/screen/find_candidates.py](skills/screen/find_candidates.py), [skills/screen/screen.md](skills/screen/screen.md) | `data/paper_items.json` | `data/paper_base_queue.json`, `data/paper_push_queue.json` |
| Rank/push | Paper Push Agent | [skills/screen/rank_push_queue.py](skills/screen/rank_push_queue.py) | `data/paper_push_queue.json`, `data/feedback.json` | selected paper, `PushedAt` |
| Download | Paper Push Agent / Python | [skills/download/paper_downloader.py](skills/download/paper_downloader.py) | DOI, title, journal | PDF under configured download directory |
| Detailed screen | Paper Push Agent | [skills/screen/screen.md](skills/screen/screen.md) | downloaded PDF text | refined Tags + Comment |
| Zotero import | Paper Push Agent / Python | [skills/zotero/zotero_import.py](skills/zotero/zotero_import.py) | paper metadata, PDF, Tags, Comment | Zotero item + PDF + note + collection |

## Quick Start

For development or maintenance:

```text
Read README.md, then agents/developer-agent.md.
```

For daily paper push:

```text
Read README.md, then agents/paper-push-agent.md.
```

## Main References

| Area | Document |
|---|---|
| Developer role and permissions | [agents/developer-agent.md](agents/developer-agent.md) |
| Paper push role and permissions | [agents/paper-push-agent.md](agents/paper-push-agent.md) |
| Fetch/search/export/merge | [skills/fetch-merge/fetch-merge.md](skills/fetch-merge/fetch-merge.md) |
| Screening, ranking, tags, comments | [skills/screen/screen.md](skills/screen/screen.md) |
| Research preferences and scoring | [memories/research_preferences.md](memories/research_preferences.md) |
| PDF download and publisher support | [skills/download/download.md](skills/download/download.md) |
| Zotero import behavior | [skills/zotero/zotero.md](skills/zotero/zotero.md) |
| Known setup pitfalls | [docs/pitfalls-and-setup.md](docs/pitfalls-and-setup.md) |
| End-to-end verification | [docs/test-plan.md](docs/test-plan.md) |

## Core Commands

```bash
# Fetch from WoS
python skills/fetch-merge/wos_cdp_workflow.py --start-date 2026-04-01 --end-date 2026-05-26

# Merge exported WoS txt files
python skills/fetch-merge/merge_exports.py --input data/source_exports/runs/<run_id>

# Find papers needing preliminary screen
python skills/screen/find_candidates.py --limit 10 --json

# List unpushed push-queue papers (batch with tags)
python skills/screen/rank_push_queue.py --unpushed --json --limit 10 --offset 0

# Download one PDF
python skills/download/paper_downloader.py --doi "10.xxx/yyy" --title "Paper Title" --journal "European Journal of Operational Research"

# Import one paper to Zotero
python skills/zotero/zotero_import.py --doi "10.xxx/yyy" --title "Paper Title" --pdf "data/pdfs/paper.pdf" --tags-json '{"Object":"..."}' --comment "..."
```

## Data And Config Files

| File | Role |
|---|---|
| `skills/fetch-merge/paper-watch.config.json` | WoS keyword groups and target journals |
| `data/paper_items.json` | merged WoS records before screening |
| `data/paper_base_queue.json` | all screened papers |
| `data/paper_push_queue.json` | high-priority papers for push |
| `data/feedback.json` | user feedback for future ranking/selection |
| `skills/download/download.config.json` | PDF download directory |

| `skills/zotero/zotero.config.json` | Zotero collection/storage/debug-bridge config |
