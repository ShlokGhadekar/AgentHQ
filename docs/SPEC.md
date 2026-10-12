# Project: AgentHQ — a multi-agent software team with a live control plane

> The original project brief. Decisions made after it (answers to open questions) are listed at the end.

## Goal
Build AgentHQ, a portfolio project for an AI engineering resume. It runs a small team of AI agents
(Manager, Researcher, Engineer, Reviewer) that take a goal about a GitHub repo, such as
"add rate limiting to /login", split it into tasks, message each other, edit code in a sandbox,
run tests, and open a PR after a human approves. A React dashboard shows the whole team live.

Priorities, in order:
1. It works end to end on a real repo, reliably.
2. Every design decision is simple enough for me to explain in an interview.
3. It looks impressive in a 2-minute demo.

Prefer clear, well-structured code over clever abstractions. Do not use agent frameworks
(no LangChain, LangGraph, CrewAI, AutoGen). I want to own and explain the orchestration logic myself.

## RepoLens integration (my existing RAG project)
RepoLens is my RAG system over codebases (hybrid BM25 + vector search over an embedded Chroma
index; answers cite files and line ranges that are checked against the retrieved code). It runs
as a FastAPI HTTP service (Python 3.13, no other services; needs `git` and one of GROQ_API_KEY or
ANTHROPIC_API_KEY) and is started with
`uvicorn repolens.api.main:app --host 0.0.0.0 --port 8000`.
A public instance runs at https://repolens-lgck.onrender.com.

- GET /status: no body → {indexed: bool, public_demo: bool, source: str|null,
  files_indexed: int|null, chunks_indexed: int, indexed_at: str|null}.
  Use it as the health check and to see which repo is loaded.
- POST /index: {"source": str} (a public GitHub URL like https://github.com/owner/repo, or a
  folder path on the RepoLens server) → {source: str, files_indexed: int, chunks_indexed: int}.
  409 if another run is in progress; 400 if the repo is missing or private, has no indexable
  files, or the path is invalid.
- GET /index/progress: no body → {active: bool, source: str|null,
  stage: "download"|"read"|"chunk"|"embed"|"save"|null, done: int, total: int,
  files: int|null, chunks: int|null}
- POST /ask: {"question": str} → {answer: str (Markdown), model: str, found: bool,
  best_similarity: float, min_similarity: float, refusal_reason: "threshold"|"model"|null,
  citations: [{path: str, start_line: int, end_line: int, language: str, code: str}],
  retrieval: [{chunk_id: str, path: str, start_line: int, end_line: int, bm25_rank: int|null,
  vector_rank: int|null, similarity: float|null, rrf_rank: int, rrf_score: float,
  rerank_score: float|null, final_rank: int|null}]}

Indexing is synchronous. POST /index blocks until it finishes (about 6 s for a 40-file repo on a
laptop; can take minutes on a slow server; the first run also downloads an 80 MB model) and
replaces the single global index. While it runs, GET /index/progress can be polled from a separate
request. There are no job IDs, and only one run can happen at a time.

Notes:
- One repository per server at a time, and /ask has no repo parameter. Check /status.source
  before asking and re-index if it's the wrong repo. Don't call /ask while indexing.
- Paths are relative to the repo root with forward slashes; lines are 1-indexed and inclusive;
  chunk IDs are "path:start-end". Citations can be sub-ranges of retrieved chunks.
- Read citations from the `citations` array, not the answer text (the model sometimes writes
  【】 instead of []).
- There is no way to get raw chunks without the LLM: citations[].code has only the cited lines,
  and `retrieval` has locations and scores but no code.
- Top-k is fixed at 6 chunks.
- "Not found" is a 200 response with found:false and answer "I could not find this in the
  repository". Treat it as a valid result, not an error.
- Errors use {"detail": str}: 400 when nothing is indexed or the source is bad, 502 when the LLM
  call fails (missing or rejected key, rate limit, outage). An empty question gives 422, where
  `detail` is a list.
- No authentication or rate limiting. Files over 300 KB, binary files, lock files, and
  vendor/build folders are skipped.
- The public instance accepts only GitHub URLs up to 500 files, shares one index across all
  callers, and can take about a minute to wake up.

### How AgentHQ must use RepoLens
- Wrap it behind a `RepoLensTool` adapter class so it can be swapped for a mock in tests.
- Run RepoLens locally as a service in AgentHQ's docker compose (port 8000). Do not use the
  public Render instance in normal runs. Make the base URL configurable via env var.
  Ask me for the path to my RepoLens repo when setting up docker compose.
- RepoLens holds one global index, so treat it as a shared resource:
  - At the start of each run, call GET /status. If `source` is not the run's repo, call
    POST /index with the repo's GitHub URL, polling GET /index/progress so the UI can show
    indexing progress.
  - Guard all RepoLens calls with a single async lock so two runs can never index or query
    at the same time. Document this limitation and how it would be solved at scale
    (an index per repo) in DECISIONS.md.
  - Never call /ask while indexing; on 409, wait and retry with backoff.
- Use generous timeouts: about 300 s for /index, about 60 s for /ask.
- RepoLensTool.ask() returns a typed result: answer, found, best_similarity and citations
  (path, start_line, end_line, code). Read file locations from `citations`, never by parsing
  the answer text.
- found:false is a normal result. The Researcher rephrases the question once, then falls back
  to searching files in the sandbox (grep / reading files).
- Since RepoLens doesn't return full chunks, the Researcher uses citations to locate code, then
  reads surrounding lines directly from the sandbox clone when it needs more context.
- Error mapping: 400 → configuration error, surfaced to the Manager; 502 → retryable LLM failure
  with backoff; 422 → bug in our call, logged and not retried.
- Record each RepoLens call as tool_call / tool_result events, including found, best_similarity
  and the cited paths, so the UI side panel and replay show what was retrieved.
- MockRepoLensTool for tests returns scripted results, including a found:false case and a 502.

## Architecture
- Backend: Python 3.11+, FastAPI, asyncio.
- Message bus: Redis Streams. Every agent has its own stream (inbox). Every message is also
  appended to a global `events` stream. The event stream is the single source of truth:
  the UI, the replay feature and the eval metrics are all built from it.
- Each agent is an async worker loop: read inbox → call LLM with its role prompt, task context
  and tools → act (call tools or send messages) → write events.
- Storage: Postgres (via SQLAlchemy) for runs, tasks, events and token usage.
- LLM: a thin `LLMClient` interface supporting Anthropic and Groq/OpenAI-compatible APIs through
  tool/function calling, selected by env var. Also provide a `FakeLLMClient` with scripted
  responses for tests and an offline demo mode.
- Sandbox: each run clones the target repo into a Docker container. The Engineer edits files
  and the Reviewer runs the test command there. Nothing touches the real repo until approval.
- GitHub: open the PR with PyGithub, only after an approval event from the human.
- Frontend: React + TypeScript + Vite, React Flow for the org graph, Tailwind for styling,
  WebSocket connection to the backend that streams events live.
- Everything starts with `docker compose up` (backend, frontend, Postgres, Redis, RepoLens).

## Agents (configured in agents.yaml: name, role prompt, model, allowed tools)
- Manager: turns the goal into 3–6 tasks with dependencies, assigns them, tracks status,
  handles failures (retry or reassign), asks the human for approval, writes the final report.
- Researcher: answers questions about the codebase using RepoLensTool and file reading.
  Sends briefs with file paths and line ranges.
- Engineer: reads and edits files in the sandbox, writes tests, can message the Researcher
  directly with questions.
- Reviewer: runs tests and a linter, reviews the diff, approves or sends it back with comments.

Agents may message each other directly (peer-to-peer), not only through the Manager.
Each agent publishes a capability card (name, description, tools) the Manager uses to assign work.

## Message / event schema
Define one Pydantic `Event` model: id, run_id, timestamp, type (task_created, task_assigned,
message, tool_call, tool_result, status_change, approval_requested, approval_decision,
llm_usage, indexing_progress, error, run_complete), sender, recipient, task_id, payload,
tokens_in, tokens_out.

## Reliability and control (important for interviews)
- Max steps per agent per task, max total steps per run, timeout per LLM call.
- Detect loops: if two agents exchange more than N messages on one task without progress,
  escalate to the Manager.
- Retries with backoff on LLM and tool errors; repeated failures escalate to the Manager.
- Token usage recorded per agent per call; a per-run token cap that stops the run cleanly.
- Pause, resume and stop for a run, from the API and the UI.
- Approval gate: opening a PR always requires an approval_decision event from the human.

## Frontend
- Org graph: agents and RepoLens as nodes, edges animate when a message passes between them,
  node color shows state (idle, thinking, waiting, error). Clicking a node opens a side panel
  with that agent's messages, current task, tool calls (including RepoLens citations) and
  token count.
- Indexing progress bar when RepoLens is indexing the target repo.
- Task board: Kanban columns (to do, in progress, review, done) updated live.
- Chat: send a goal to the Manager, or message any single agent directly.
- Approval card: shows the diff and test results with Approve and Reject buttons.
- Replay: pick a past run and step through its events, with the graph and board reconstructed
  at each step.
- Simple stats bar: tokens per agent, total cost estimate, elapsed time.

## Evaluation harness (this is the headline resume metric)
- Include a small sample repo in the project (for example a small FastAPI app with tests),
  also pushed as a public GitHub repo so RepoLens can index it. Ask me for the URL once I've
  pushed it.
- `evals/` with 8–10 tasks on that repo. Each task has a goal and a pass condition
  (tests passing, specific behaviour).
- Run each task in two modes: single-agent baseline (one agent with all tools) and team mode.
- Output a markdown report with success rate, average tokens, average cost and average time
  per mode.

## Build process
Work in these phases. At the end of each phase, run the tests, show me how to verify it
manually, and stop for my confirmation before continuing.
1. Project skeleton, docker compose (Postgres, Redis, RepoLens), Event model, LLMClient and
   FakeLLMClient.
2. Agent runtime and bus: Manager delegating to workers, visible as logs in the terminal,
   working with FakeLLMClient.
3. Real tools: RepoLensTool (with lock, re-index check and error mapping), sandbox file editing,
   test running. Full run on the sample repo with a real LLM.
4. Reliability and control: limits, loop detection, retries, token caps, pause/stop,
   approval gate, GitHub PR.
5. Frontend: graph, side panel, task board, chat, approval card, indexing progress.
6. Replay and stats.
7. Evaluation harness and report.
8. Documentation.

## Testing
Pytest unit tests for the bus, event model, task state transitions, limits and RepoLensTool
error handling (using MockRepoLensTool), plus one integration test that runs a full scripted run
with FakeLLMClient. Keep tests fast.

## Documentation (I need to explain this in interviews)
- README.md: what it is, a GIF placeholder, architecture diagram (Mermaid), how to run,
  eval results table.
- DECISIONS.md: for every major choice (Redis Streams vs alternatives, no agent framework,
  event sourcing, peer-to-peer messaging, sandboxing, approval gates, the RepoLens lock and
  single-index limitation, eval design), write the choice, the alternatives considered, the
  trade-offs, and why. Written so I can say it aloud.
- INTERVIEW_NOTES.md: likely interview questions about this project with concise answers,
  covering failure handling, scaling to more agents, cost control, preventing agent loops,
  how RAG fits into the agent workflow, and what I would do differently.

## Code style
Type hints everywhere, small functions, docstrings on public classes, comments explaining
"why" rather than "what". No unused abstractions. If something is ambiguous, ask me rather
than guessing.

---

## Decisions made after the brief
- RepoLens source lives at `../RepoLens`. Compose builds it with an AgentHQ-owned Dockerfile
  (`docker/repolens/Dockerfile`), so the RepoLens repo itself is not modified.
- Default LLM provider for real runs: Groq (OpenAI-compatible API). Anthropic stays supported via env var.
- Git: one commit at the end of each phase, after confirmation.
