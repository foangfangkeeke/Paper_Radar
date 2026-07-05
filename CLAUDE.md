# Paper Radar

Two roles — do not mix.

| Task | Role | Read first |
|---|---|---|
| Code, bugs, docs, config, infrastructure | Developer | `agents/developer-agent.md` |
| Paper screening, ranking, push, download, Zotero | Paper Push Agent | `agents/paper-push-agent.md` |

When the user says "push" or asks about papers, act as Paper Push Agent.
Read `memories/research_preferences.md` for current scoring preferences.
When the user asks about code, architecture, or fixes, act as Developer.

Pipeline: fetch-merge -> screen -> rank/push -> download -> detailed screen -> zotero import
