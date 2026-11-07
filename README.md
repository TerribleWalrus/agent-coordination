# agent-coordination (v1.0.0)

A Claude skill that lets several AI agents (Claude Code sessions, possibly
belonging to different people on different machines) work in the same git
repository at the same time without stepping on each other.

Agents coordinate through an append-only log on an orphan branch,
`communication`, in the repo you already share. They lock the files or
functions they edit, settle disagreements with a fixed lazy-consensus
procedure, split work into tasks, review each other, and recover work from
sessions that crashed, hit usage limits or were abandoned. Humans only see
the decisions that are genuinely theirs, plus a digest afterwards.

* **No server, database or extra credential.** If an agent can push code, it
  can coordinate.
* **One file, standard library only.** `scripts/coord.py` needs git and
  Python 3.8+.
* **Never touches your working tree or current branch.** The bus is read and
  written with git plumbing.

## Install into a repository (once per repo)

```bash
mkdir -p .claude/skills
cp -r agent-coordination .claude/skills/          # or unzip agent-coordination.skill there
python3 .claude/skills/agent-coordination/scripts/coord.py install
git add .claude CLAUDE.md && git commit -m "Add agent coordination" && git push
```

`install` writes Claude Code hooks into `.claude/settings.json` and adds a
short note to `CLAUDE.md`. From then on, every Claude Code session opened in
the repo:
* registers itself on the bus automatically, or resumes its previous
  identity in that checkout;
* gets a briefing on who else is working and what they hold;
* receives coordination messages after its tool calls;
* has its unfinished work autosaved to `wip/<agent>`.

Nobody has to mention the skill. Without the hooks the skill still works, but
agents have to run `join`, `inbox` and `wait` themselves.

Recommended:
* In CI, ignore the bus branches: `on: push: branches-ignore: [communication, 'wip/**']`.
* To make the pre-push guard require an approved review before anything
  lands on `main`, run once: `python3 .claude/skills/agent-coordination/scripts/coord.py policy require-review main`.
* All agents on one machine and you don't want the traffic on GitHub? Point
  `COORD_REMOTE` at a local bare repo.

## Watching as a human

Use your own checkout and identity. Reading marks messages as read for the
identity that reads them, so never read with an agent's identity.

```bash
export COORD_AGENT=me-observer
python3 .claude/skills/agent-coordination/scripts/coord.py board     # who holds what, queues, tasks
python3 .claude/skills/agent-coordination/scripts/coord.py digest    # what needs you, what was decided
```

Or open `BOARD.md` on the `communication` branch in GitHub. To answer an
escalation, join as a human (`join --agent <you> --role human`) and
`say --re <seq> "<answer>"`. Humans can post but never vote or review.

## What's inside

| path | what it is |
|---|---|
| `SKILL.md` | what agents read: the loop, the rules, conflict avoidance, beginner-human guidance, recovery |
| `references/playbook.md` | fixed procedures for reviews, breaking changes, incidents, handoffs, releases… |
| `references/commands.md` | the full command reference and environment variables |
| `references/protocol.md` | design, event model, guarantees and limits |
| `scripts/coord.py` | the tool (`--help` on every command, `--version`) |
| `tests/test_coord.py` | 29 end-to-end tests against a throwaway bare remote: `python3 tests/test_coord.py` |

## How it was built

Four rounds of simulated teams shaped it. The first three used AI agents
told to follow the skill. The fourth used real Claude Code sessions driven
by deliberately inexperienced "humans" who never mentioned the skill. Every
failure seen in those runs is now a regression test. The scenarios covered:
* four to six agents fighting over the same function;
* a crash while holding the hottest lock;
* a full sprint with a breaking migration, an incident, contradictory
  stakeholders and a release;
* beginners with vague, conflicting requests, an 8-feature wish list,
  laptops closed mid-task and usage limits.

The research log, with all bus logs, lives in the source repository under
`research/agent-coordination/`.

## Known limits

* Safety nets (autosave, guards, orphan detection) only work where this
  version of the tool runs. Older clients see a TOOL OUTDATED notice.
* An agent that has ended its turn cannot be woken by the bus. Hooks deliver
  news only while it is working.
* Throughput is bounded by git push round-trips: fine for tens of agents,
  not hundreds.
* Visual or cross-feature regressions (e.g. a button unreadable in one theme)
  still need a human or a visual check.
