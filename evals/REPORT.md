# AgentHQ evaluation

10 tasks on the sample repo, each run by the four-agent team and by a single agent with all the same tools. A task passes when the repo's own tests and the task's hidden acceptance tests pass in a fresh sandbox. Costs are estimates at list prices; the runs themselves used Groq's free tier.

| Mode | Tasks scored | Success rate | Avg tokens | Avg cost | Avg time | Avg LLM calls |
|---|---|---|---|---|---|---|
| team | 0 | – | – | – | – | – |
| solo | 0 | – | – | – | – | – |

Models: team: openai/gpt-oss-120b, qwen/qwen3.8-27b; solo: openai/gpt-oss-120b

## Per task

| Task | Difficulty | Team | Solo |
|---|---|---|---|
| health_endpoint | easy | not run | not run |
| password_min_length | easy | not run | not run |
| title_whitespace | easy | not run | not run |
| me_endpoint | easy | not run | not run |
| search_notes | medium | not run | not run |
| update_note | medium | not run | not run |
| pagination | medium | not run | not run |
| logout | medium | not run | not run |
| delete_account | hard | not run | not run |
| rate_limit_login | hard | not run | not run |

Pass/fail reasons, outcomes and run ids are in `results.json`; every run can be replayed in the UI.
