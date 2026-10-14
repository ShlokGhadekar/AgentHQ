# Design decisions

Each decision: what I chose, what else I considered, what it costs, and why I still chose it.
They are written to be said out loud.

---

## 1. No agent framework

**Choice.** The orchestration is plain Python and asyncio: an agent is a loop around an LLM client,
tools are Pydantic models plus a handler, and the run is one class with one event pipeline.

**Alternatives.** LangGraph, CrewAI, AutoGen.

**Trade-offs.** I wrote things a framework gives you for free: the tool-calling loop, retries,
message passing, state. It took longer, and there is no ecosystem of ready-made integrations.

**Why.** The interesting parts of a multi-agent system are exactly the parts a framework hides:
who decides what happens next, what happens when an agent fails or loops, how state stays
consistent when four agents act at once. I wanted to be able to point at the line of code that
makes each of those decisions. The core runtime (agent loop, run, tools, task board, bus,
guardrails) is about 1,400 lines, so the cost of owning it is small, and nothing in it is magic.

---

## 2. Event sourcing: one stream is the source of truth

**Choice.** Everything that happens is an `Event` (one Pydantic model, twelve types). State is
never edited directly: the task board, the dashboard, replay and the evaluation metrics are all
computed by folding the events in order. The backend's `TaskBoard.apply` and the frontend's
`reduceEvents` are the same idea in two languages.

**Alternatives.** Keep mutable state in objects or database rows, and log events on the side.

**Trade-offs.** Every piece of state needs a reducer, and reducers have to be kept in step with the
event types. Reading the current state means replaying, which is fine at this scale (a run is a
few hundred events) but would need snapshots for very long runs.

**Why.** It makes three hard features almost free. The live UI is the reducer applied to events
as they arrive; replay is the same reducer applied to the first n events; eval metrics are sums
over the log. It also makes debugging honest: a test rebuilds the board from the Redis stream and
checks it equals the board the run ended with, so the log provably holds everything.

---

## 3. Redis Streams for the message bus

**Choice.** Each agent has an inbox stream per run, and every event also goes to one global
`agenthq:events` stream. Both writes happen in one Redis transaction. Each agent reads its inbox
with a plain cursor (`XREAD` from the last id it saw).

**Alternatives.**
- *asyncio queues in memory*: simplest, but nothing survives a restart and no other process can watch.
- *Redis pub/sub*: fire and forget; a subscriber that is down misses messages, and there is no history.
- *Kafka or RabbitMQ*: built for this, but another heavy service to run for a one-machine project.
- *Postgres LISTEN/NOTIFY*: notifications aren't stored and carry small payloads.

**Trade-offs.** Redis is one more service, and streams are kept in memory (with append-only
persistence on disk), so they need trimming eventually.

**Why.** A stream is an append-only log with ids: a reader that remembers its last id can't miss or
double-read an entry, and the history stays readable, which is what replay and the recorder need.
I used a plain cursor instead of consumer groups because each inbox has exactly one reader.
Consumer groups pay off when several workers share one inbox, for example a pool of engineers,
and that is how I would scale a role.

---

## 4. One publish path, under one lock

**Choice.** `Run.publish` is the only way anything happens. Under one asyncio lock it applies the
event to the board (rejecting illegal moves), writes it to Redis, runs the guardrails, and works
out the follow-ups (hand out unblocked tasks, tell the Manager something failed).

**Alternatives.** Finer-grained locks per task, or an actor per task.

**Trade-offs.** It serialises all event handling in a run. That is not a bottleneck: the slow
part is LLM calls, which happen outside the lock.

**Why.** Without it, two agents finishing at the same moment could both see a task as ready and
hand it out twice, or the event log could record events in a different order than the board saw
them. One lock makes "the board and the log always agree" true by construction, and it is easy
to explain.

---

## 5. The LLM plans, code schedules

**Choice.** The Manager decides the tasks, their assignees and their dependencies. A plain-code
scheduler then hands each task out the moment its dependencies are done, along with their
results. The Manager is only woken when a task fails or when everything is done.

**Alternatives.** Let the Manager LLM poll and assign each task itself.

**Trade-offs.** The Manager can't make clever mid-flight changes unless something fails.

**Why.** Handing out an unblocked task needs no judgement, so spending tokens and seconds on it is
waste, and an LLM can forget or double-assign where code can't. It also cuts the Manager's token
use to a few calls per run (about 5k to 10k tokens in the live runs).

---

## 6. Peer-to-peer messaging

**Choice.** Agents can message each other directly. The Engineer asks the Researcher a question
and gets the answer in its own inbox, without going through the Manager. Reviews go straight
from the Reviewer back to the Engineer.

**Alternatives.** A strict hub-and-spoke where every message goes through the Manager.

**Trade-offs.** Direct conversations can loop, and the Manager doesn't see every exchange.

**Why.** Routing a question through the Manager doubles the hops and the tokens and adds a point
where context gets lost. The loop risk is real, so it has a guard (decision 9): too many messages
between two agents about one task without the task moving forward fails the task, which hands
the decision back to the Manager. Every message is still an event, so nothing is invisible.

---

## 7. Conversation per task, fresh on reassignment, inside a token budget

**Choice.** Each agent keeps one conversation per task. When a task is handed out again (a retry)
the conversation starts empty, with the Manager's note and the dependency results. Before every
LLM call, old tool outputs are replaced by a short placeholder until the request fits a budget
(4,500 tokens by default), always keeping the newest two in full. If the provider still says the
request is too large, the budget shrinks and the call is retried.

**Alternatives.** One long conversation per agent; summarising old turns with another LLM call.

**Trade-offs.** An agent sometimes re-reads a file whose earlier read was removed.

**Why.** Learned the hard way in live runs. A retried task that kept its old context failed
instantly with the same "request too large" error, and the Manager retried it into the same wall.
Tool outputs (file contents, test logs) are most of the context and are the cheapest to get back,
so they go first. Summarising would cost another LLM call per turn.

---

## 8. Turn-ending tools

**Choice.** Some tools end the agent's turn the moment they succeed: `create_tasks`,
`submit_task`, `approve_task`, `request_changes`, `request_approval`, `finish_run`.

**Alternatives.** Always send the tool result back to the LLM and let it decide to stop.

**Trade-offs.** The agent can't add a remark after submitting; it never needs to.

**Why.** After submitting a task there is nothing left for that agent to do, so asking the LLM
"anything else?" is a wasted call per task. It also makes runs more predictable.

---

## 9. Guardrails counted from events

**Choice.** One module (`guards.py`) watches the event stream and reports crossed limits:

| Limit | Default | What happens |
|---|---|---|
| LLM calls per agent per task attempt | 30 | the task fails; the Manager decides |
| LLM calls per run | 200 | the run stops |
| Tokens per run | 400,000 | the run stops |
| Retries of one task | 3 attempts | `retry_task` refuses |
| Messages between two agents about a task, without progress | 8 | the task fails |
| Times the Reviewer sends a task back | 3 | the task fails |
| Failed tool calls in a row | 6 | the task fails |

Temporary LLM failures (rate limits, server errors, timeouts) are retried with exponential
backoff, waiting as long as the provider's Retry-After says. A wait longer than 60 seconds means a
quota is used up, so it fails at once instead.

**Alternatives.** Counters scattered through the agent loop; a single global timeout.

**Trade-offs.** Limits are blunt: a genuinely long task can hit the step limit.

**Why.** Counting from events means the counts are exactly what the log shows, and replay can
explain why a limit fired. Splitting task-level from run-level limits matters: a stuck task is a
decision for the Manager (retry, reassign, give up), while a blown token budget is something
nothing inside the run can fix. If the Manager itself fails, the run ends with the reason
instead of hanging, because nobody is left to decide.

---

## 10. Sandboxing: edit in a workspace, execute in a container

**Choice.** Each run clones the repo into its own workspace folder. Agents read and edit files
through Python methods that refuse any path outside the clone or inside `.git`. Running code
(tests, the linter) happens in a Docker container per run that mounts the workspace, gets the
network only while installing the repo's dependencies, and then runs disconnected, with 1 GB of
memory, 2 CPUs and a process limit.

**Alternatives.** Do everything inside the container through `docker exec`; a remote sandbox
service (E2B, Modal); gVisor or Firecracker for stronger isolation.

**Trade-offs.** The backend starts containers through the Docker socket, which is root-equivalent
on the host. Fine for a local tool, not for a multi-tenant service. Dependencies the agents add
mid-run can't be installed, because the network is off.

**Why.** Editing files is not dangerous; running LLM-written code is. Splitting the two keeps file
operations fast and testable without Docker, and puts the isolation exactly where the risk is.
For a hosted version I would move execution to a remote sandbox service or a gVisor runtime and
drop the socket mount.

---

## 11. The approval gate

**Choice.** When all tasks are done, the Manager calls `request_approval`. The system then runs the
test suite itself and attaches the full diff, and the human approves or rejects. Only one function
opens pull requests, and it refuses unless the event log holds an approving decision. No agent has
a tool that can push. A rejection's comment goes back to the Manager, which can plan follow-up
tasks. The GitHub token reaches git only through environment variables (never the URL or the
command line) and is scrubbed from error messages.

**Alternatives.** Trust the agents' own test reports; let an agent open the PR; open a draft PR
automatically and let the human merge.

**Trade-offs.** A human must be present, so runs pause at the end. The evaluation harness stops
there by design, since that is where a human would review.

**Why.** An agent saying "tests pass" is a claim; the system re-running them is evidence, so the
approval card shows evidence. Making the gate a check on the event log, not a flag a tool could
set, means even a confused or manipulated agent can't publish anything.

---

## 12. RepoLens as a shared resource, and its single-index limit

**Choice.** RepoLens holds one global index per server. The adapter treats it as a shared
resource: every call runs under one asyncio lock, and every question first checks `/status` and
re-indexes if another run replaced the index in the meantime. Errors map to behaviour: 400 is a
configuration error that fails the task so the Manager hears about it; 502, timeouts and 409 are
retried with backoff; 422 is our bug and is logged, not retried. If RepoLens stays unavailable the
agent is told to use `search_code` and `read_file` instead. A "not found" answer allows one rephrase,
then the tool refuses and points to search. Locations always come from the structured citations,
never from parsing the answer text.

**Alternatives.** Call RepoLens freely; give each run its own RepoLens instance.

**Trade-offs.** All RepoLens use in the process is serialised, and two runs on different repos
would keep re-indexing each other's repo (about 10 to 40 seconds each time for small repos).

**Why.** Correctness first: without the lock and the per-question check, run A could get answers
about run B's repository, which is worse than slow. At scale I would change RepoLens to keep one
index per repository, keyed by repo URL and commit, and take the repo as a parameter on `/ask`.
Then the lock and the re-index check disappear and runs on different repos proceed in parallel.

---

## 13. Postgres as a copy of the stream

**Choice.** One background recorder follows the global Redis stream and writes every event to
Postgres. It resumes from a saved cursor, and inserting an event that is already stored does
nothing, so each event gets exactly one row. The `runs` table is also derived from events: the
first event of a run carries its goal and repository, and `run_complete` carries the report.

The brief listed tasks and token usage as stored data too. I deliberately didn't store them:
tasks are rebuilt from events by the same reducer the UI uses, and token usage is a sum over
`llm_usage` events.

**Alternatives.** Write to Postgres directly from the runtime (a second write on every event);
store tasks and usage in their own tables.

**Trade-offs.** History lags the live stream by up to a second, and asking for a task's state
means replaying the run's events.

**Why.** Writing to Redis and Postgres from the runtime is the dual-write problem: one can succeed
while the other fails and they drift. With one writer that follows the stream, Postgres is always
a faithful copy and can be rebuilt from Redis at any time. Storing tasks and usage again would
create two sources of truth that could disagree with the events; deriving them can't.

---

## 14. A thin LLM client, with retries in the runtime

**Choice.** One method, `complete(system, messages, tools, model)`, with a client for Anthropic and
one for OpenAI-compatible APIs (Groq, Gemini, Cerebras: one class, different base URLs), plus a
scripted `FakeLLMClient` for tests and the offline demo. The SDKs' own retries are turned off.

**Alternatives.** Use one SDK everywhere; use a multi-provider library.

**Trade-offs.** Each provider's quirks have to be handled in our code.

**Why.** Retries belong where they can be seen and counted: in the runtime, as events, under the
guardrails. And the quirks were real. Groq rejects a whole response with HTTP 400 when the model
calls a tool it doesn't have (`tool_use_failed`) or produces unparseable output
(`output_parse_failed`). Both are bad samples, not bad requests, so they are retried. Its 413
"request too large" counts more than the prompt tokens, so it triggers the shrink-and-retry from
decision 7. Gemini 3 attaches a `thought_signature` to every tool call and rejects the next request
with a 400 unless it is echoed back, so tool calls carry opaque `provider_data` that the client
returns untouched. Gemini also puts its rate-limit wait in the error body (`retryDelay`) rather than
a `Retry-After` header. The fake client is per agent, so concurrent agents can't take each other's
scripted replies, and it reports its own model name so a demo never looks like real model output.

---

## 15. Choosing models under free-tier quotas

**Choice.** The model is a per-agent setting (`provider:` and `model:` in `agents.yaml`). The
Engineer, which does the hardest work, uses `gemini-3.5-flash-lite`; the Manager, Researcher and
Reviewer use the cheaper `gemini-3.1-flash-lite`. RepoLens stays on Groq (`gpt-oss-20b`). The solo
baseline in the evaluation uses the Engineer's model.

**Alternatives.** One model for everyone; a paid tier.

**Trade-offs.** Mixed models make behaviour less uniform. Free tiers change often, so the setup
is tied to today's limits.

**Why.** It was the difference between some runs and none. The project started on Groq, where each
model gets 8,000 tokens per minute and 200,000 per day: a run took 20 to 25 minutes, mostly waiting
for the per-minute limit, and one model's daily quota ran out partway through a run. Cerebras needed
a payment method. Gemini's free tier counts requests rather than tokens, which suits agents that make
many small calls, and a run dropped to a few minutes. `gemini-3-flash-preview` was the strongest model
tried, but its free tier allows only 20 requests per day, and a live run ran out mid-task, so the
agents moved to the Flash-Lite models. On a paid tier I would give every agent the same strong model
and remove the question.

---

## 16. Evaluation design

**Choice.** Ten tasks on the sample repo, from a health endpoint to login rate limiting. Each goal
states the exact interface. A task passes when, in a fresh sandbox, the repo's whole test suite
passes and then the task's hidden acceptance tests pass. Those tests are copied in only after the
run, don't depend on the repo's own test fixtures, and run in their own process. Every task has a
reference solution, and a test proves each acceptance test fails without it and passes with it.
Each task runs twice: by the team, and by one agent with every tool the team has, on the
Engineer's model, under the same runtime, guardrails and token cap. Team runs stop at the sign-off
request; nothing is approved. Metrics (tokens, cost, time, LLM calls) come from the event log.

**Alternatives.** Let an LLM judge the result; reuse an external benchmark such as SWE-bench; count
the agents' own test runs as the pass condition.

**Trade-offs.** Ten tasks on one small repo is not a benchmark; it measures this system on this
kind of change. The tasks were written by me, so they may suit the design.

**Why.** Hidden tests are what make the number mean something: the agents can't write tests that
merely agree with their own code. Reference solutions guard the guard. Without them, one broken
acceptance test would quietly turn a pass into a fail for every run. Giving the baseline the same
tools, model and limits isolates the one thing being compared: a team structure versus one agent.
The harness saves results per task and mode and marks quota failures as "not scored", so the
evaluation can be spread across days on a free tier without corrupting the numbers.
