# AgentHQ

A multi-agent software team (Manager, Researcher, Engineer, Reviewer) with a live control plane.
Full documentation comes in Phase 8; the original brief is in [docs/SPEC.md](docs/SPEC.md).

## Run

```bash
cp .env.example .env        # then add GROQ_API_KEY (or ANTHROPIC_API_KEY with LLM_PROVIDER=anthropic)
docker compose up -d --build
curl localhost:8080/health  # Postgres, Redis and RepoLens should all report "ok": true
```

Then open http://localhost:3000 and start a run. Tick "Scripted demo" to watch the team without an API key.
Every run is recorded in Postgres, so past runs stay in the run list after a restart; pick one and
press Replay to step through it event by event.

RepoLens is built from `../RepoLens` (change `REPOLENS_PATH` in `.env` if it lives elsewhere).

## Develop

```bash
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/python scripts/llm_smoke.py   # one real tool-calling round trip with your configured provider
```

## Frontend development

```bash
cd frontend
npm install
npm run dev    # http://localhost:5173, forwards /api and /ws to the backend on :8080
npm test       # reducer tests (vitest)
```

## Watch the team work in the terminal (offline, scripted LLM)

Needs Redis from docker compose. Run from `backend/`:

```bash
.venv/bin/python -m agenthq.cli run "Add rate limiting to /login" --demo
.venv/bin/python -m agenthq.cli tail   # in a second terminal: every event of every run, read from Redis
```
