# Paper Radar

Automated academic paper discovery and screening system for research literature monitoring.

## Overview

Paper Radar monitors Web of Science (WoS) for new publications matching your research directions, screens them using the MiniMax LLM, and produces a prioritized push queue of relevant papers.

## Workflow

```
fetch -> merge -> screen -> push
```

1. **Fetch**: Browser automation logs into WoS and exports search results as Plain Text files
2. **Merge**: Parses and deduplicates exports into `data/wos_minimax_items.json`
3. **Screen**: MiniMax LLM scores and tags each paper against your research directions
4. **Push**: Generates a Markdown digest from high-scoring papers

## Research Directions

Papers are screened against four research directions:

| Direction | Description |
|-----------|-------------|
| `transport_energy_integration` | EVs, charging infrastructure, smart grids, energy-system optimization |
| `transport_emergency_resilience` | Transit disruption, emergency response, service recovery |
| `or_ai` | AI/ML for operations research, learning to optimize, MIP solvers |
| `ai_behavior` | Travel behavior modeling, choice modeling, mode choice |

## Quick Start

### 1. Configure

Edit `automation/paper-watch.config.json`:
- Set your WoS credentials in `automation/wos.config.json`
- Configure MiniMax API key in `automation/minimax.config.json`

### 2. Run Weekly Update

```bash
python automation/agent.py weekly-update --start-date 2024-01-01 --end-date 2024-12-31
```

This will:
1. Fetch WoS exports for the date range
2. Merge exports into a single JSON
3. Run a dry-run screen preview
4. Prompt to continue with actual MiniMax screening

### 3. Commands

```bash
# Fetch WoS exports via browser automation
python automation/agent.py fetch --start-date 2024-01-01 --end-date 2024-12-31

# Merge downloaded txt files
python automation/agent.py merge

# Screen papers with MiniMax
python automation/agent.py screen --dry-run
python automation/agent.py screen

# Generate push digest
python automation/agent.py push --count 5
```

## Directory Structure

```
.
├── automation/
│   ├── agent.py                    # Workflow orchestrator CLI
│   ├── wos_browser_tool.py         # WoS Selenium automation
│   ├── wos_fetch_runner.py         # Fetch entry point
│   ├── merge_exports.py           # Parse and deduplicate WoS exports
│   ├── minimax_screening_tool.py   # MiniMax LLM screening logic
│   ├── screen_items.py            # Screen entry point
│   ├── push_items.py             # Generate Markdown digest
│   ├── wos_to_minimax_runner.py  # Combined fetch+screen runner
│   ├── paper-watch.config.json   # Research directions, keywords, journals
│   ├── minimax.config.json       # MiniMax API configuration
│   └── wos.config.json           # WoS browser settings
├── data/
│   ├── wos_exports/              # Downloaded WoS txt files
│   ├── paper_base_queue.json     # All screened papers
│   ├── paper_push_queue.json     # High-scoring papers for review
│   └── paper-screening-cache.json # Screening results cache
└── reports/
    ├── inbox/                    # Drop WoS txt files here (optional)
    └── archive/                  # Archived processed files
```

## Configuration

### paper-watch.config.json

| Field | Description |
|-------|-------------|
| `keywords` | WoS search queries for each research direction |
| `topJournals` | Target journal list (filtered in screening) |
| `llmScreening.minLlmScore` | Minimum score (0-10) to enter push queue (default: 6) |
| `llmScreening.batchSize` | Papers per MiniMax API call (default: 10) |

### minimax.config.json

```json
{
  "apiKey": "your-minimax-api-key",
  "model": "MiniMax-M2.7",
  "batchSize": 10,
  "timeoutSec": 120
}
```

### wos.config.json

| Field | Description |
|-------|-------------|
| `account` | WoS account email |
| `password` | WoS password |
| `downloadDir` | Where to save exported txt files |
| `browserProfileDir` | Chrome profile for session persistence |
| `exportChunkSize` | Records per export batch (default: 1000) |

## Output Files

- **`data/wos_minimax_items.json`**: Merged and deduplicated paper metadata
- **`data/paper_base_queue.json`**: All screened papers with scores and tags
- **`data/paper_push_queue.json`**: Papers scoring above threshold, sorted by recommendation score
- **`reports/push_YYYY-MM-DD.md`**: Markdown digest for review
