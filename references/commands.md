# coord.py command reference (v1.0.0)

`$C` = `python3 .claude/skills/agent-coordination/scripts/coord.py`. Every
command prints `--help`. Write commands fetch the bus first, then push one
event, retrying automatically if someone else wrote at the same moment.

## Presence

| command | notes |
|---|---|
| `join --agent N --human H --task T [--branch B] [--role agent\|human] [--session ID] [--no-hooks] [--history]` | Registers you, or resumes the identity stored in this checkout. Installs the git guards (pre-commit claim guard, pre-push freeze/review guard) unless `--no-hooks`. `--role human` is for stakeholders: they can post, but never vote or review. |
| `status "note" [--task T]` | Updates your status line; also a heartbeat. |
| `renew` | Extends your leases (any write does too). |
| `leave [--note N] [--unmerged REASON]` | Releases everything. Refuses while your claimed files differ from main, unless `--unmerged` (which creates a `STRANDED-…` task). |
| `adopt OLD [-m why] [--force]` | Takes over a stale agent's claims (keeping queue position), tasks and open review requests. Allowed immediately for the same `--human`. |

## Claims (locks)

| command | notes |
|---|---|
| `claim RES... -m intent [--ttl 20m] [--shared] [--urgent] [--no-queue]` | Atomic over all of `RES`. Returns `GRANTED` or `QUEUED` (FIFO), and refuses claims that would deadlock. Also prints `RECENT WORK` by others on the same files, and when queued, the conflicting pairs. |
| `release [IDs or RES] [-m note] [--all] [--unmerged REASON]` | Defaults to all of yours. The note is what landed ("merged abc: …"). Refuses while your claimed files differ from main, unless `--unmerged`. |
| `guard [--staged] [--strict] [--base origin/main] [--push]` | Checks your diff against others' claims (exit 1 on conflict). Runs as the pre-commit (`--staged`) and pre-push (`--push`) hooks. |

Resource syntax:

| resource | meaning |
|---|---|
| `path` | a file |
| `dir/` | a subtree |
| `src/*.py` | a glob |
| `path#region` | a function, class or section |
| `@name` | an abstract resource |
| `@freeze:<branch>` | blocks other agents' pushes to that branch |

Two claims conflict when they belong to different agents, at least one is
exclusive, their paths overlap, and their regions are equal or either has no
region.

## Decisions

| command | notes |
|---|---|
| `propose TITLE --about RES… \| --topic T --default D [--deadline 5m] [--body B] [--needs-human] [--reopen] [--force]` | One open proposal per resource or topic; similar open titles are refused (`--force` overrides). Decided questions are refused unless `--reopen`. |
| `vote PID accept\|object [--alt TEXT\|A<n>] [--reason R]` | An objection needs an alternative; `A<n>` backs an existing one. You cannot vote on your own proposal. |
| `withdraw PID` | Retracts your own open proposal. |
| `decisions` | Lists every proposal with votes and outcome. |

Resolution: the proposer and silent voters count for the default, and
objectors count for their alternative. The plurality wins and ties go to the
default. A proposal resolves at its deadline, or as soon as every live voter
has voted. Agents who leave drop out of the count.

## Tasks

| command | notes |
|---|---|
| `task add "ID: title files=a,b deps=T1" ... [--spec TEXT]` | Ids are unique (CAS). Deps must be task ids. The spec is the verbatim requirement. |
| `task take ID [--force]` | Prints the spec. Refused if a live agent owns the task, or if its deps are not done (`--force` overrides deps). A stale owner's task can be taken. |
| `task done ID [-m sha]` | Prints the tasks this unblocked. |
| `task drop ID [--to agent\|any] [-m state]` | Hands the task off (the creator may also drop an untaken task). |
| `task spec ID "text"` | Appends to the acceptance criteria. |
| `tasks` (or `task list`) | Lists the board. `ORPHAN-…` and `STRANDED-…` tasks are created automatically for abandoned or unmerged work. |

## Reviews

| command | notes |
|---|---|
| `review request [--to A] [--ref BR] [--task ID] [-m note] [--self] [--force]` | Auto-assigns the least-loaded live agent. Refused if the branch has no diff against main. `--self` is allowed only when nobody else is live, and is audited. |
| `review approve R [-m]` / `review changes R -m "<required change>"` | Reviewer only. |
| `review reassign R [--to A]` | Author only: when the reviewer is stale, or after 10 minutes. |
| `review withdraw R` | Author only. |
| `review list [--all]` | Lists open reviews, or every review with `--all`. |
| `policy require-review main` / `policy require-review` (clears) / `policy show` | Team policy. The pre-push guard refuses a push to a listed branch unless its change matches an approved review. |

## Messages and waiting

| command | notes |
|---|---|
| `say TEXT --to all\|a,b [--kind info\|question\|answer\|decision\|handoff\|contract\|blocker\|incident\|requirement] [--re SEQ] [--needs-human]` | Warns if a recipient has left or is stale, and lists the tasks they left. |
| `inbox [--peek]` | Shows unread items for you and marks them read (unless `--peek`). |
| `wait [--claim C] [--proposal P] [--task T] [--review R] [--timeout 100s] [--interval 10s]` | Blocks until the condition holds or relevant news arrives, and returns early on actionable items. Exit 0 = something happened; exit 2 = timeout (call it again). Heartbeats while waiting. |

## Reading state

| command | notes |
|---|---|
| `board [--markdown]` | Agents (active, stale, offline, left), claims, queues, tasks, open proposals, the waits-for graph. |
| `log [-n 20]` | Raw recent events. |
| `digest [--since 24h]` | For humans: what needs them, answered escalations, decisions, work, integrations, traffic. |

## Claude Code integration

| command | notes |
|---|---|
| `install` | Writes the hooks below into `.claude/settings.json` and a note into `CLAUDE.md`. Idempotent. Commit both. |
| `session-start` | SessionStart hook: auto-joins or resumes this checkout's identity and injects a briefing (who is live, what you own, unfinished work). |
| `hook [--event E]` | PostToolUse / UserPromptSubmit hook: delivers unread events into context, heartbeats live owners, autosaves to `wip/<agent>`, and warns a superseded session. Rate-limited; never fails the tool call. |
| `session-end` | SessionEnd hook: autosaves and marks you offline (your work is kept for resume or adoption). |
| `install-hooks` | (Done by `join`.) Installs the git pre-commit and pre-push guards. |

## Environment

| variable | default | meaning |
|---|---|---|
| `COORD_REMOTE` | `origin` | the remote that holds the bus |
| `COORD_BRANCH` | `communication` | the bus branch (use one per team or project if needed) |
| `COORD_AGENT` | identity file | overrides the identity for one command (observers, scripts) |
| `COORD_TTL` | `1800` | claim lease in seconds; renewed by any activity |
| `COORD_LIVE` | `900` | seconds of silence before an agent counts as stale |
| `COORD_HOOK_INTERVAL` | `30` | minimum seconds between hook runs |

Identity, read cursors and caches live in `.git/coord/` of each checkout or
worktree, never in the repo.
