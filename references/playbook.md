# Team events playbook

Each section is a fixed procedure, so that no negotiation is needed when the
event happens. `$C` is `python3 .claude/skills/agent-coordination/scripts/coord.py`.

## Contents
- [Code review](#code-review)
- [Breaking change to a shared API](#breaking-change-to-a-shared-api)
- [Production incident / hotfix / red main](#production-incident--hotfix--red-main)
- [Requirement change from a human](#requirement-change-from-a-human)
- [Contradictory instructions from two humans](#contradictory-instructions-from-two-humans)
- [Leaving mid-task, reassignment, handoff](#leaving-mid-task-reassignment-handoff)
- [Late join](#late-join)
- [Planning a multi-agent effort](#planning-a-multi-agent-effort)
- [Release](#release)
- [Last agent standing](#last-agent-standing)

## Code review

Applies when the team requires it. `join` prints `team policy:
require_review=[main]`, and the pre-push guard then refuses pushes to main
without an approval.

**Author**
1. Commit, `git pull --rebase origin main`, run the tests.
2. `git push origin HEAD:<your-branch>`.
3. `$C review request --task <ID> -m "<what changed and why>"`. A live
   reviewer is auto-assigned (the least busy one). `--task` shows them the
   task's verbatim acceptance criteria.
4. `$C wait --review R<n>`, looping on timeout. Keep your claim meanwhile.
5. Approved: `git push origin HEAD:main`. The approval covers exactly this
   diff (a zero-context patch-id), so it survives a clean rebase. If you
   change code, or resolve conflicts after approval, request again.
6. Changes requested: fix them, then `review request --to <same reviewer>`.
7. Reviewer went stale (`REVIEWER STALE` in your inbox):
   `$C review reassign R<n>`. Wrong request: `$C review withdraw R<n>`.

**Reviewer.** Answer review requests before starting new work, because
someone is blocked on you.
1. `git fetch origin <branch> && git diff origin/main...FETCH_HEAD`, then run
   the tests.
2. Judge the change against the task's acceptance criteria, not the author's
   summary. Specs drift through handoffs, and the review is where that gets
   caught.
3. `$C review approve R<n> -m "<what you checked>"`, or
   `$C review changes R<n> -m "<the concrete change required>"`.

## Breaking change to a shared API

A rename, type change or new signature that other agents' code depends on.
Never do it as one big change under one giant claim: everyone else's
in-flight work would break. Use expand → migrate → contract:
1. `$C propose "<change>" --about <api resource> --default "<old -> new, migration plan>"`.
2. **Expand:** merge the new API *alongside* the old one, so the old one keeps
   working.
3. `$C say --kind contract "<exact new usage>"`. Each owner migrates their own
   call sites, or you do it under normal claims. Track each one as a task.
4. **Contract:** remove the old API last, after `grep` shows no callers on
   main, and say so in the release note.

## Production incident / hotfix / red main

1. Whoever sees it first: `$C say --kind incident "<what is broken>"` and
   `$C task add "HOTFIX-1: <fix>" --spec "<the report, verbatim>"`.
2. The first `task take HOTFIX-1` wins. Take the task *before* you claim or
   announce anything; never announce ownership you have not won.
3. The owner claims the code with `--urgent`, which puts it ahead of normal
   queued claims and pings the holders. If the fix must ship before anything
   else, the owner also claims `@freeze:main`. The pre-push guard then holds
   every other merge until the owner releases the freeze, right after the fix
   lands.
4. Holders who see `URGENT WAITING ON YOU`: commit or stash, release at the
   next safe point, and re-claim afterwards.
5. Review hotfixes before anything else.
6. Red main: whoever broke it fixes or reverts within minutes. Nobody merges
   on top of red main except the fix.

## Requirement change from a human

A human message on the bus, or one relayed by an agent, overrides earlier
agent decisions on that point. The owner of the affected code implements it.
If nobody owns it, `task add` it with the human's words as `--spec`. Use
`propose --reopen` only when the change needs a new design choice between
agents.

## Contradictory instructions from two humans

For example, one says the threshold is 40 and another says 60, or one says
dark and another says pink.
1. Don't pick a side and don't overwrite. If both wishes can coexist (a
   toggle, a setting, a separate page), build that, including the other
   agent's work.
2. Otherwise keep the current behaviour for the contested item and escalate:
   `$C say --needs-human "<both instructions and the current state>"` or
   `propose ... --needs-human`.
3. Carry on with everything else. When a human answers on the bus, implement
   it. They may never answer; the default stands.

## Leaving mid-task, reassignment, handoff

1. Make sure the task carries the original requirement verbatim:
   `$C task spec <ID> "<paste>"`.
2. Commit and push your branch (autosave also keeps `wip/<agent>`).
3. `$C task drop <ID> --to <agent|any> -m "branch X; done: …; left: …"`.
4. `$C release --unmerged "handoff, see <ID>"` (if the work is not on main),
   then `$C leave`.

The receiver runs `$C task take <ID>` (it prints the spec) and continues from
the pushed branch.

## Late join

`join` prints the board. Read `$C decisions` and `$C tasks`, and take open,
handed-off, `ORPHAN-…` or `STRANDED-…` tasks before inventing new work.

## Planning a multi-agent effort

If you are first and the work splits:
`$C task add "T1: … files=… " "T2: … deps=T1" --spec "<verbatim>"`. Dependencies
must be task ids; a dependent task cannot be taken until its deps are done
(unless you pass `--force` to build against a published contract). If
`task add` fails because the ids exist, someone planned first: use their
plan.

## Release

1. Check that the sprint is actually complete: no open tasks, proposals or
   reviews, every agreed change on main, and no new requirements pending.
2. `$C claim @freeze:main -m "release X.Y.Z"` and announce a short freeze.
3. Bump the version, cut the changelog, run the tests, get the release
   reviewed if required, push, and tag (`git tag vX.Y.Z && git push origin vX.Y.Z`).
4. `$C release -m "released vX.Y.Z"` lifts the freeze.

Agents blocked by the freeze keep working locally and push afterwards.

## Last agent standing

If review is required and no other agent is live, wait a reasonable time
(`$C wait`) or escalate. If the work cannot wait:
`$C review request --self -m "<what you verified>"`. It is allowed only when
nobody else is live, and it is listed under "Needs a human" in
`$C digest` for audit.
