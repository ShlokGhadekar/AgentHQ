# Interview notes

Likely questions about AgentHQ, with short answers to say out loud. The longer reasoning behind
each design choice is in [DECISIONS.md](DECISIONS.md).

---

## The project

**Give me the 60-second version.**
AgentHQ is a team of four LLM agents (Manager, Researcher, Engineer, Reviewer) that takes a goal
for a GitHub repo and works on it like a small dev team. The Manager plans tasks with dependencies,
the Researcher uses my RAG system RepoLens to find the relevant code, the Engineer edits a sandboxed
clone and runs the tests, the Reviewer checks the diff, and a human has to approve before a pull
request opens. I built the orchestration myself in Python and asyncio, on Redis Streams, with every
event recorded so the React dashboard can show it live and replay any past run. It has guardrails
against loops and runaway cost, and an evaluation harness that compares the team against a single
agent on ten tasks with hidden tests.

**What happens, step by step, when I submit a goal?**
The backend creates a run: it clones the repo into a workspace, starts a sandbox container, and
makes sure RepoLens has the repo indexed. Then the goal goes into the Manager's inbox. The Manager
calls `create_tasks`. From there plain code hands each task to its agent as soon as its
dependencies are done. Agents work through tool calls, message each other directly, and submit.
Engineering tasks go to the Reviewer automatically. When everything is done, the Manager asks for
sign-off; the system re-runs the tests and shows the diff; if I approve, it pushes a branch and
opens the PR. Every one of those steps is an event on one stream.

**Why build it without LangGraph or CrewAI?**
Because the interesting decisions in a multi-agent system are the ones frameworks hide: who acts
next, what happens when an agent fails, how state stays consistent with four agents running at once.
I wanted to point at the exact line that makes each decision. The core runtime is about 1,400 lines,
so owning it was cheap.

---

## Failure handling

**What happens when an agent fails?**
It depends on the failure. A temporary LLM error (rate limit, server error, timeout) is retried with
exponential backoff, honouring the provider's Retry-After; each retry is an event, so it shows in the
UI. A mistake the model can fix, like a bad file path or wrong arguments, goes back to the model as
the tool result. Anything else fails the agent's current task with the reason. A failed task wakes the
Manager, which can retry it, retry it with someone else plus a note, or finish the run with an
honest report. A retry starts with a fresh conversation, so it doesn't inherit whatever broke the
first attempt. If the Manager itself fails, the run ends with the reason, because nobody is left
to decide. In a live run I watched exactly that path: the Engineer hit a daily quota twice, and the
Manager ended the run explaining the quota, instead of hanging.

**What if the server crashes in the middle of a run?**
Today the run stops and is shown as "interrupted". Nothing is lost: every event is in Redis and
copied to Postgres, so I can replay it, and the next start removes the orphaned sandbox containers.
What I don't do yet is resume. Because the state is a fold over the event log, resuming is
mostly "rebuild the board from the events and re-deliver unread inbox messages". For that I'd switch
the inboxes to Redis consumer groups, which track delivered-but-unacknowledged messages.

**What happens when the LLM provider misbehaves?**
Real examples from Groq. It rejects a whole response with a 400 when the model calls a tool it
doesn't have, or produces output it can't parse; those are bad samples, so I retry them. It answers
413 "request too large" based on its own token counting; I keep a context budget, and if it still
complains I shrink harder and retry. And when a daily quota runs out, the wait is minutes long, so
retrying is pointless: anything over 60 seconds fails immediately instead of burning time.

---

## Preventing loops

**How do you stop agents from going in circles?**
Several guards, all counted from the event stream:
- If two agents exchange more than 8 messages about one task without it changing status, the task
  fails as a possible loop and the Manager decides what to do.
- The Reviewer can send a task back at most 3 times.
- One agent gets at most 30 LLM calls per task attempt, and the whole run 200.
- 6 failed tool calls in a row fail the task.
- The Manager can retry a task at most 3 times.
- If an agent ends its turn just describing what it would do, it gets one reminder; a second time,
  its task fails.

The pattern is: a stuck task becomes the Manager's decision, and only budget-level problems stop
the whole run.

---

## Cost

**How do you control cost?**
Three things.
1. **Don't spend tokens on what code can do.** Task scheduling is plain code, so the Manager
   only thinks when something fails or finishes; in live runs it used 5k to 10k tokens per run.
   Tools like `submit_task` end the turn, saving a call per task.
2. **Keep each request small.** Every task has its own conversation, and old tool outputs are dropped
   first when the context gets too big.
3. **Hard limits.** A token cap per run stops it cleanly.

Every LLM call records its estimated cost in the event, so the dashboard shows live spend per agent,
and the evals compare cost between modes. Models are set per agent, so the cheap roles can use
cheap models.

**What does a run actually cost?**
In the evaluation a team run averaged about 100k tokens, which is about 3 cents at Gemini Flash-Lite
list prices, and took one to three minutes. I ran it on free tiers. Most tokens go to the Engineer,
whose context carries files and test output; the Manager used only 2k to 5k per run because code
does the scheduling.

---

## Scaling

**How would you scale to more agents, or many runs at once?**
- **More agents per role:** switch inboxes to consumer groups so several engineers share one queue.
- **Many runs:** each run's state lives in one backend process today. To spread runs across
  machines, I'd pin each run to a worker (its events already live in Redis keyed by run, so any
  process can read them) and move the run registry into Redis or Postgres.
- **The real bottleneck is RepoLens:** it holds one index per server, so I serialise all calls and
  re-check which repo is loaded before every question. At scale, it should keep one index per repo
  and commit and take the repo as a parameter; then runs on different repos stop blocking each other.
- **Sandboxes:** a remote sandbox service instead of the local Docker socket.
- **The stream:** trim Redis once events are safely in Postgres, and partition the events table by run.

**Why Redis Streams and not Kafka?**
For one machine, Kafka is a lot of operations for little gain. Redis Streams give me the property I
need: an append-only log with ids, so readers can't miss messages and history is replayable. Pub/sub
wouldn't keep history; asyncio queues wouldn't survive a restart or let another process watch.

---

## RAG in the agent workflow

**How does RAG fit in?**
RepoLens is my hybrid BM25-plus-vector RAG service. The Researcher calls it as a tool,
`ask_codebase`. Answers come with structured citations (file and line range), and I only take
locations from those, never from parsing the prose. RepoLens returns just the cited lines, so the
Researcher then reads the surrounding code with `read_file`. If RepoLens can't find something, the
Researcher may rephrase once, and then the tool tells it to fall back to `search_code`, so it
doesn't burn tokens retrying. If RepoLens is down, the same fallback kicks in. Each query and its
citations are recorded, so the UI shows exactly what was retrieved. In live runs it found the login
endpoint at `app/auth.py:26-34` on the first question.

**Why not just give the model the whole repo?**
For a small sample repo you could, but it doesn't scale, and it wastes the context budget, which on
some free tiers is 8,000 tokens per minute. Retrieval gives the agent a map; reading the cited
lines gives it the detail.

---

## Safety

**How do you stop the agents from doing damage?**
- **Editing:** file tools refuse any path outside the clone or inside `.git`.
- **Running code:** happens only in a container that has no network after installing dependencies,
  with memory, CPU and process limits.
- **Pushing:** no agent has a tool that can push. One function opens pull requests, and it refuses
  unless the event log holds a human approval.
- **The token:** the GitHub token is passed to git through environment variables, never in a URL,
  and is scrubbed from errors.

The weak spot I'd fix for a hosted version: the backend uses the Docker socket, which is
root-equivalent on the host.

**Why does the system re-run the tests at approval time?**
Because an agent saying "tests pass" is a claim, not evidence. The approval card shows a diff and
test results the system produced itself.

---

## Testing and evaluation

**How do you know it works?**
There are 150 backend tests that run in about 5 seconds with no services, using an in-memory Redis
and scripted LLM clients. They include full scripted team runs, and one checks that rebuilding the
task board from the event stream gives exactly the run's final board. Further tests cover the real
Docker sandbox, the frontend reducer, and every eval acceptance test against its reference solution.
Then there were live runs with real models on the sample repo. Those got through planning,
retrieval, edits and sandboxed tests, and every failure I saw turned into a fix and a test.

**What did the live runs teach you?**
That everything around the model is the hard part. Real examples:
- A model wrote `line_start` instead of `start_line`. Pydantic silently ignored it, so the tool read
  the whole file every time; tool arguments now reject unknown fields.
- A retried task kept its old, oversized context and failed instantly with the same error; retries
  now start fresh.
- When the Manager crashed, the run hung for 25 minutes; now it ends.
- Gemini 3 rejects a tool call unless its `thought_signature` is sent back, so tool calls now
  carry opaque provider data through the conversation.
- Free-tier quotas ran out mid-run twice; the runtime now spots a daily quota and fails fast.

**How did you design the evaluation?**
Ten tasks on the sample repo, each with hidden acceptance tests that the agents never see. A task
passes only if the repo's own tests and the hidden tests pass in a fresh sandbox. Every task has a
reference solution, and a test proves each acceptance test fails without it and passes with it,
so a broken test can't skew results. The baseline is one agent with every tool the team has, the
Engineer's model, and the same limits, so the only difference is team structure.

**So is the team better than a single agent?**
On my ten tasks both solved everything, 10 out of 10, so the difference is efficiency. The team used
fewer tokens on 7 of 10 tasks, 103k against 126k on average, and was faster, 1.3 against 1.8
minutes. The reason is context: each agent has a short conversation about one task, while the single
agent drags every file and test log it has seen through one long conversation. On logout that was
46k tokens against 176k. Where the team lost was the hardest task: the Reviewer sent the Engineer's
work back over an unused variable in a test, and that extra round made it 332k against 230k. That's
the honest trade-off of a review step: it costs tokens every time and only pays off when it catches
something that matters. What I'd do next is harder tasks, because when both modes score 100% the
benchmark can't tell you which one is more reliable, and more than one run per task, because
single runs are noisy.

---

## Looking back

**What would you do differently?**
- **Budget for evaluation from day one.** Free-tier quotas shaped too much of the project; the
  whole evaluation costs well under a dollar at list prices.
- **Design for resume from the start:** consumer groups for inboxes, and a run that can rebuild
  itself from its events.
- **Typed event payloads per event type** instead of a free-form dict. It kept the schema flexible,
  but the Python and TypeScript reducers have to agree by convention.
- **A shared test for the two reducers**, Python and TypeScript, run on the same recorded event
  logs, so they can't drift.
- **One index per repo in RepoLens**, which removes the global lock.

**Hardest bug?**
Two stand out. On the backend: a retried task kept failing with "request too large" because it
inherited the failed attempt's conversation, so the Manager kept retrying a task that could never
succeed. The fix was a fresh conversation on reassignment plus a context budget with
shrink-and-retry. On the frontend: after switching runs the graph went blank. React Flow hides a
node until it has measured it, my rebuilt nodes lost their measurements, and React Flow only
re-measures when a node's size changes. I keep the measurements and hand them back.
