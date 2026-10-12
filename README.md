# AgentHQ

A multi-agent software team (Manager, Researcher, Engineer, Reviewer) with a live control plane.
Full documentation comes in Phase 8; the original brief is in [docs/SPEC.md](docs/SPEC.md).

## Run

```bash
cp .env.example .env        # then add GROQ_API_KEY (or ANTHROPIC_API_KEY with LLM_PROVIDER=anthropic)
docker compose up -d --build
curl localhost:8080/health  # Postgres, Redis and RepoLens should all report "ok": true
```

RepoLens is built from `../RepoLens` (change `REPOLENS_PATH` in `.env` if it lives elsewhere).

## Develop

```bash
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/python scripts/llm_smoke.py   # one real tool-calling round trip with your configured provider
```
