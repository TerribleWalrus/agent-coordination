# Changelog

## 1.0.0 (2026-11-07)

First release. It consolidates the development iterations, internal tool
versions 1.0 to 4.2, each driven by a simulated multi-agent run. The research
log is in `research/agent-coordination/` of the source repository.

**Bus.** An append-only event log on the orphan branch `communication`.
Every write is a compare-and-swap push, which gives a total order and means
locks and tasks are never double-granted.

**Claims**
* Files, directories, globs, `path#region` and `@pseudo` resources.
* Atomic multi-claims, a FIFO queue with urgent priority, and deadlock
  refusal by simulation.
* TTL leases with heartbeat.
* `RECENT WORK` and conflict explanations in `claim` output.

**Decisions**
* Lazy consensus: one open proposal per resource or topic, labelled
  alternatives, deadlines with early resolution, and final outcomes.
* `--needs-human` escalation.

**Tasks**
* Unique ids, dependencies, verbatim acceptance criteria (`--spec`) that
  travel through handoffs into reviews, and `drop --to` handoffs.

**Reviews**
* Least-loaded live reviewer assignment, reassignment of stale reviewers,
  withdraw.
* Approvals bound to change content (zero-context patch-id).
* `policy require-review` enforced by the pre-push guard.
* Audited self-review for the last agent standing.

**Liveness and recovery**
* Live and stale detection; resume of an identity per checkout; detection
  of a superseded session; `adopt`.
* Autosave to `wip/<agent>`.
* ORPHAN tasks for abandoned claims and STRANDED tasks for declared
  unmerged work.
* A release/leave guard while claimed files differ from main.

**Claude Code integration**
* `install` writes SessionStart (auto-join and briefing), PostToolUse and
  UserPromptSubmit (delivery, heartbeat, autosave) and SessionEnd (offline)
  hooks.
* Git pre-commit (claim guard) and pre-push (freeze and review guard) hooks.

**Humans**
* `--role human` stakeholders, `digest` with "Needs a human" and answered
  escalations, `BOARD.md` on the branch.

**Skill**
* `SKILL.md` covers the core loop and rules, conflict avoidance, guidance
  for agents of inexperienced humans, and recovery. The details are in
  `references/` (playbook, commands, protocol).
