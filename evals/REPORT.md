# AgentHQ evaluation

10 tasks on the sample repo, each run by the four-agent team and by a single agent with all the same tools. A task passes when the repo's own tests and the task's hidden acceptance tests pass in a fresh sandbox. Costs are estimates at list prices; the runs themselves used free tiers.

| Mode | Tasks scored | Success rate | Avg tokens | Avg cost | Avg time | Avg LLM calls |
|---|---|---|---|---|---|---|
| team | 10 | 10/10 (100%) | 103,251 | $0.0334 | 1.3 min | 26 |
| solo | 10 | 10/10 (100%) | 126,425 | $0.0415 | 1.8 min | 21 |

Models: team: gemini-3.1-flash-lite, gemini-3.5-flash-lite; solo: gemini-3.5-flash-lite

## Per task

| Task | Difficulty | Team | Solo |
|---|---|---|---|
| health_endpoint | easy | pass, 36k tokens, 0.5 min | pass, 47k tokens, 1.0 min |
| password_min_length | easy | pass, 61k tokens, 0.6 min | pass, 106k tokens, 1.2 min |
| title_whitespace | easy | pass, 59k tokens, 0.6 min | pass, 72k tokens, 1.0 min |
| me_endpoint | easy | pass, 88k tokens, 0.7 min | pass, 148k tokens, 1.9 min |
| search_notes | medium | pass, 41k tokens, 0.6 min | pass, 60k tokens, 4.5 min |
| update_note | medium | pass, 48k tokens, 0.5 min | pass, 90k tokens, 0.5 min |
| pagination | medium | pass, 60k tokens, 1.2 min | pass, 59k tokens, 0.6 min |
| logout | medium | pass, 46k tokens, 2.4 min | pass, 176k tokens, 3.1 min |
| delete_account | hard | pass, 332k tokens, 3.4 min | pass, 230k tokens, 2.0 min |
| rate_limit_login | hard | pass, 262k tokens, 2.3 min | pass, 276k tokens, 2.3 min |

Pass/fail reasons, outcomes and run ids are in `results.json`; every run can be replayed in the UI.
