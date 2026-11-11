# Group Day Planner

A hackathon demo of agents that plan a San Francisco day out for a group (two friends and a dog), built on **Archil** branches and sandboxes, with **Keenable** web search and **OpenInstinct**-style personal agents (simulated as two phones in the browser).

- Each person keeps their limits (walking, hills, diet, museum time, weather) in their own profile and shares only a summary, with consent.
- The planner combines everyone's limits, searches for cafes, museums, food and beaches, then forks one branch per museum-and-beach pair to simulate routes: walking time with hills, opening hours, dog rules and the weather at sunset.
- With `PLANNER_ROUTES=sandboxes`, every route branch runs in its own **Archil sandbox** (35 microVMs per request, forked from one base sandbox in about a second).
- A judge picks up to 3 different plans that leave nobody unhappy; both people vote or ask for changes ("different beach", "less walking").

### How the demo uses disk branching and sandboxes

Each route simulation runs in its own Archil sandbox. The group's preferences are branched out to every sandbox:

1. Everyone's limits and preferences, plus the candidate venues, are written to one base sandbox. That sandbox is then stopped, so it acts as a checkpoint.
2. The planner forks that base once per simulation (one per museum-and-beach pair). Each fork is a copy-on-write branch of the base's disk, so it starts with all the preferences already in place and nothing is copied.
3. Each sandbox simulates its routes independently and writes its results only to its own branch. The winning sandboxes are kept as `plan-1` to `plan-3`; the rest are deleted.

The center of the demo has three screens that show this while it runs:

| Screen | Shows |
|---|---|
| **Map** | The selected plan's route across San Francisco, with numbered stops and each person's scores and picks |
| **Routes** | Each plan as a timetable: arrival and departure times, walking or riding between stops, distance, climb, and notes such as dog rules or fog |
| **Technical** | A live timeline of the disk checkpoints, the search branches, the base sandbox and every route sandbox (fork, then run), with fork times, storage written compared with full copies, and the sandboxes kept as plans |

## Run it

Python 3.9+ on macOS. No packages to install for the local demo.

```bash
cp .env.example .env    # optional: add ARCHIL_API_KEY, ARCHIL_REGION, PLANNER_SEARCH, PLANNER_ROUTES
python3 server.py       # then open http://localhost:8765
```

| Setting in `.env` | Effect |
|---|---|
| `PLANNER_SEARCH=keenable` | Check each venue's hours, location and dog rules with Keenable (keyless works, rate-limited) |
| `PLANNER_ROUTES=sandboxes` | Run each route branch in its own Archil sandbox (needs `ARCHIL_API_KEY`, `ARCHIL_REGION`, `ARCHIL_DISK_ID`) |
| `ANTHROPIC_API_KEY` | Use Claude web search to find venues |

## Tests

```bash
python3 run_tests.py                      # local copy-on-write branches, one per scenario
python3 run_tests.py --sandboxes --keenable  # one Archil sandbox per scenario, live Keenable search
python3 run_tests.py --archil             # real Archil disk branches inside one Archil microVM
```

Tests always use fixed seed data and local routes unless a test opts in (F2 live search, E8 routes on sandboxes).

## Layout

| Path | What |
|---|---|
| `planner/planner.py` | The planning flow: consent, combine, search branches, route branches, judge, votes, changes |
| `planner/routes.py` | Route simulation and group scoring |
| `planner/disk.py` | Local copy-on-write and Archil disk branches |
| `planner/sandbox_routes.py`, `planner/route_worker.py` | Route branches on Archil sandboxes |
| `planner/keenable.py`, `planner/search.py` | Venue search |
| `web/index.html` | The two-phone demo page with Map, Routes and Technical tabs |
| `tests/` | Unit tests, scenario tests, and the sandbox test runner |

Seed venue data is illustrative; check hours and dog rules before you go.
