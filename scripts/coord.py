#!/usr/bin/env python3
"""coord - a git-backed coordination bus for teams of AI agents.

All coordination state lives on an orphan branch (default: ``communication``)
as an append-only log of small JSON events. Every write is a compare-and-swap
``git push`` of exactly one new commit on top of the current tip, so the
remote acts as the sequencer: events get a total order (``seq``) and two
agents can never both be granted the same lock.

Nothing here touches your working tree, index or current branch: the bus is
read and written with git plumbing only, so agents can use it from any
branch or worktree, on any machine that can reach the remote.

Stdlib only. Python 3.8+. Run ``coord.py --help`` for commands; the
agent-facing protocol is in ../SKILL.md, the full reference in
../references/commands.md.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

VERSION = "1.0.0"
BRANCH = os.environ.get("COORD_BRANCH", "communication")
REMOTE = os.environ.get("COORD_REMOTE", "origin")
LOCAL_REF = f"refs/coord/{BRANCH}"
DEFAULT_TTL = int(float(os.environ.get("COORD_TTL", 30 * 60)))  # lease seconds; renewed by activity
DEFAULT_DEADLINE = 10 * 60     # proposal deadline, seconds
# An agent silent for longer than this is "stale": not counted in votes, not
# assigned reviews, and its tasks may be taken over. Live sessions heartbeat
# through the Claude Code hook, so this can be short.
LIVE_WINDOW = int(float(os.environ.get("COORD_LIVE", 15 * 60)))
ACTIVE_WINDOW = LIVE_WINDOW


class CoordError(Exception):
    pass


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------

def git(*args, input=None, check=True, env=None):
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    p = subprocess.run(["git", *args], input=input, capture_output=True,
                       text=True, env=full_env)
    if check and p.returncode != 0:
        raise CoordError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p


def git_out(*args, **kw):
    return git(*args, **kw).stdout.strip()


_STATE_DIR = None


def state_dir():
    """Per-worktree private dir for identity, cursor and caches."""
    global _STATE_DIR
    if _STATE_DIR is None:
        gd = git_out("rev-parse", "--absolute-git-dir")
        _STATE_DIR = os.path.join(gd, "coord")
        os.makedirs(_STATE_DIR, exist_ok=True)
    return _STATE_DIR


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def fetch(quiet_fail=False):
    """Fetch the bus branch. Returns tip sha or None if it does not exist."""
    last = None
    for attempt in range(5):
        p = git("fetch", "-q", "--no-tags", REMOTE,
                f"+refs/heads/{BRANCH}:{LOCAL_REF}", check=False)
        if p.returncode == 0:
            return git_out("rev-parse", LOCAL_REF)
        err = p.stderr.lower()
        if "couldn't find remote ref" in err or "could not find remote ref" in err:
            git("update-ref", "-d", LOCAL_REF, check=False)
            return None
        last = p.stderr.strip()
        time.sleep(2 ** attempt)
    if quiet_fail:
        p = git("rev-parse", "--verify", "-q", LOCAL_REF, check=False)
        return p.stdout.strip() or None
    raise CoordError(f"cannot fetch {REMOTE}/{BRANCH}: {last}")


def load_events(tip):
    if not tip:
        return []
    out = git_out("ls-tree", "-r", tip, "--", "events")
    entries = []
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        _, _, sha = meta.split()
        entries.append((path, sha))
    cache_path = os.path.join(state_dir(), "event-cache.json")
    cache = read_json(cache_path, {})
    missing = [sha for _, sha in entries if sha not in cache]
    if missing:
        p = subprocess.run(["git", "cat-file", "--batch"],
                           input=("\n".join(missing) + "\n").encode(),
                           capture_output=True, check=True)
        data = p.stdout
        i = 0
        for sha in missing:
            nl = data.index(b"\n", i)
            header = data[i:nl].split()
            size = int(header[2])
            body = data[nl + 1: nl + 1 + size]
            i = nl + 1 + size + 1
            cache[sha] = json.loads(body.decode())
        write_json(cache_path, cache)
    events = [cache[sha] for _, sha in entries]
    events.sort(key=lambda e: e["seq"])
    return events


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------

def now():
    return time.time()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(t, ref=None):
    d = int((ref or now()) - t)
    if d < 0:
        return f"in {fmt_dur(-d)}"
    return f"{fmt_dur(d)} ago"


def fmt_dur(d):
    d = int(d)
    if d < 90:
        return f"{d}s"
    if d < 5400:
        return f"{d // 60}m"
    return f"{d // 3600}h{(d % 3600) // 60:02d}m"


def parse_dur(s):
    s = str(s).strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in mult:
        return float(s[:-1]) * mult[s[-1]]
    return float(s)


# --------------------------------------------------------------------------
# resources & conflicts
# --------------------------------------------------------------------------

def split_res(r):
    r = r.strip()
    if "#" in r:
        path, region = r.split("#", 1)
        return norm_path(path), region.strip() or None
    return norm_path(r), None


def norm_path(p):
    p = p.strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def paths_overlap(p, q):
    if p == q:
        return True
    if p.startswith("@") or q.startswith("@"):
        return False
    if p.endswith("/") and q.startswith(p):
        return True
    if q.endswith("/") and p.startswith(q):
        return True
    if any(ch in p for ch in "*?[") and fnmatch.fnmatch(q, p):
        return True
    if any(ch in q for ch in "*?[") and fnmatch.fnmatch(p, q):
        return True
    return False


def res_overlap(a, b):
    pa, ra = split_res(a)
    pb, rb = split_res(b)
    if not paths_overlap(pa, pb):
        return False
    return ra is None or rb is None or ra == rb


def claims_conflict(c1, c2):
    if c1["agent"] == c2["agent"]:
        return False
    if c1["mode"] == "shared" and c2["mode"] == "shared":
        return False
    return any(res_overlap(a, b) for a in c1["resources"] for b in c2["resources"])


# --------------------------------------------------------------------------
# replay: fold the event log into current state
# --------------------------------------------------------------------------

class State:
    def __init__(self):
        self.seq = 0
        self.clock = 0.0
        self.agents = {}
        self.claims = {}      # id -> claim (insertion order == seq order)
        self.msgs = []
        self.props = {}
        self.tasks = {}
        self.reviews = {}
        self.policy = {}
        self.events = []
        self.transitions = []  # (t, kind, payload) derived happenings

    # ---- time-driven transitions --------------------------------------
    def advance(self, t):
        if t <= self.clock:
            return
        self.clock = t
        while True:
            changed = False
            for c in self.claims.values():
                if c["status"] in ("active", "queued") and c["expires"] <= t:
                    was_active = c["status"] == "active"
                    c["status"] = "expired"
                    c["ended"] = c["expires"]
                    self.transitions.append((c["expires"], "expired", c["id"]))
                    changed = True
                    if was_active and not c["resources"][0].startswith("@"):
                        self._unfinished(f"ORPHAN-{c['id']}", c["agent"], c["expires"],
                                         f"finish or redo {c['agent']}'s abandoned work: {c['intent']}",
                                         f"{c['agent']} stopped responding while holding "
                                         f"{', '.join(c['resources'])} (lease lapsed {iso(c['expires'])}, "
                                         f"never released). Its autosave, if any: `git fetch origin "
                                         f"wip/{c['agent']}` then `git cherry-pick FETCH_HEAD` (or merge "
                                         f"it) and resolve conflicts keeping both sides - never `git "
                                         f"checkout <wip> -- <file>`, which silently drops newer work on "
                                         f"main. Redo it if there is no autosave, or `task drop` with a "
                                         f"note if it is not wanted.",
                                         c["resources"])
            for p in self.props.values():
                if p["status"] == "open" and p["deadline"] <= t:
                    self.resolve(p, p["deadline"])
            if changed:
                self.promote(t)
            else:
                break

    def _unfinished(self, tid, agent, t, title, spec, files):
        if tid in self.tasks:
            return
        self.tasks[tid] = {"id": tid, "title": title, "files": [split_res(r)[0] for r in files],
                           "deps": [], "by": agent, "owner": None, "status": "open",
                           "note": "", "created": t, "spec": spec, "unfinished": True}
        self.transitions.append((t, "unfinished", tid))

    def promote(self, t):
        """Grant queued claims, FIFO, whose blockers are gone."""
        active = [c for c in self.claims.values() if c["status"] == "active"]
        waiting_before = []
        queued = sorted((c for c in self.claims.values() if c["status"] == "queued"),
                        key=lambda c: (not c.get("urgent"), c["seq"]))
        for c in queued:
            blockers = [a for a in active if claims_conflict(a, c)]
            # FIFO: earlier queued conflicting claims go first - unless that
            # earlier claim is itself waiting on *this* agent (it could never
            # be granted before us, so honouring it would deadlock).
            ahead = [w for w in waiting_before if claims_conflict(w, c) and not any(
                a["agent"] == c["agent"] and claims_conflict(a, w) for a in active)]
            if not blockers and not ahead:
                c["status"] = "active"
                c["granted"] = t
                c["expires"] = t + c["ttl"]
                c["blocked_by"] = []
                active.append(c)
                self.transitions.append((t, "granted", c["id"]))
            else:
                c["blocked_by"] = [b["id"] for b in blockers + ahead]
                waiting_before.append(c)

    def resolve(self, p, t):
        tally = {}
        electorate = [a for a in p["electorate"] if a != p["agent"]]
        tally[p["default"]] = 1  # proposer
        for a in electorate:
            v = p["votes"].get(a)
            if v is None or v["stance"] == "accept":
                choice = p["default"]
            else:
                choice = p["alts"].get(v.get("alt"), v.get("alt")) or "REJECT"
            tally[choice] = tally.get(choice, 0) + 1
        best = max(tally.values())
        winners = [k for k, n in tally.items() if n == best]
        outcome = p["default"] if p["default"] in winners else sorted(winners)[0]
        p["tally"] = tally
        p["outcome"] = outcome
        p["status"] = "rejected" if outcome == "REJECT" else "decided"
        p["resolved"] = t
        self.transitions.append((t, "resolved", p["id"]))

    # ---- event application --------------------------------------------
    def apply(self, e):
        t = max(e["t"], self.clock)
        self.advance(t)
        self.seq = max(self.seq, e["seq"])
        self.events.append(e)
        a = e["agent"]
        typ = e["type"]
        ag = self.agents.get(a)
        if ag:
            ag["last_seen"] = t
            ag["offline"] = False
        # any activity renews this agent's leases
        for c in self.claims.values():
            if c["agent"] == a and c["status"] in ("active", "queued"):
                c["expires"] = t + c["ttl"]
        fn = getattr(self, "on_" + typ, None)
        if fn:
            fn(e, t)

    def on_join(self, e, t):
        prev = self.agents.get(e["agent"])
        if prev:  # same identity, new session: resume, keep everything it owns
            prev.update({"status": "active", "last_seen": t, "offline": False,
                         "session": e.get("session") or prev.get("session"),
                         "sessions": prev.get("sessions", 1) + 1})
            for k in ("human", "task", "branch"):
                if e.get(k):
                    prev[k] = e[k]
            if e.get("role"):
                prev["role"] = e["role"]
            return
        self.agents[e["agent"]] = {
            "name": e["agent"], "human": e.get("human", ""),
            "task": e.get("task", ""), "branch": e.get("branch", ""),
            "joined": t, "last_seen": t, "status": "active",
            "role": e.get("role", "agent"), "session": e.get("session"), "sessions": 1,
        }

    def is_live(self, name, t=None):
        ag = self.agents.get(name)
        t = self.clock if t is None else t
        return bool(ag) and ag["status"] == "active" and not ag.get("offline") and \
            t - ag["last_seen"] < LIVE_WINDOW

    def on_offline(self, e, t):
        ag = self.agents.get(e["agent"])
        if ag:
            ag["offline"] = True

    def on_adopt(self, e, t):
        old, new = e["from"], e["agent"]
        for c in self.claims.values():
            if c["agent"] == old and c["status"] in ("active", "queued"):
                c["agent"] = new
                c["expires"] = t + c["ttl"]
                c["adopted_from"] = old
        for task in self.tasks.values():
            if task["owner"] == old and task["status"] == "taken":
                task["owner"] = new
        for r in self.reviews.values():
            if r["status"] == "open" and r["author"] == old:
                r["author"] = new
        ag = self.agents.get(old)
        if ag:
            ag["status"] = "left"
            ag["note"] = f"adopted by {new}"
        self.promote(t)

    def on_status(self, e, t):
        ag = self.agents.get(e["agent"])
        if ag:
            if e.get("task"):
                ag["task"] = e["task"]
            ag["note"] = e.get("note", "")

    def on_leave(self, e, t):
        ag = self.agents.get(e["agent"])
        if ag:
            ag["status"] = "left"
        self._stranded(e, t, [c for c in self.claims.values() if c["agent"] == e["agent"]
                              and c["status"] in ("active", "queued")])
        self._release(lambda c: c["agent"] == e["agent"], t, e.get("note", "left"))
        for task in self.tasks.values():
            if task["owner"] == e["agent"] and task["status"] == "taken":
                task["owner"] = None
                task["status"] = "open"
        # agents who leave stop counting; their silence no longer blocks early resolution
        for p in self.props.values():
            if p["status"] == "open" and e["agent"] in p["electorate"] and e["agent"] != p["agent"]:
                if e["agent"] not in p["votes"]:
                    p["electorate"].remove(e["agent"])
                self._maybe_resolve_early(p, t)

    def on_claim(self, e, t):
        c = {
            "id": f"C{e['seq']}", "agent": e["agent"],
            "resources": e["resources"], "mode": e.get("mode", "exclusive"),
            "intent": e.get("intent", ""), "ttl": e.get("ttl", DEFAULT_TTL),
            "created": t, "status": "queued", "expires": t + e.get("ttl", DEFAULT_TTL),
            "blocked_by": [], "seq": e["seq"], "urgent": e.get("urgent", False),
        }
        self.claims[c["id"]] = c
        self.promote(t)

    def _release(self, pred, t, note):
        hit = []
        for c in self.claims.values():
            if c["status"] in ("active", "queued") and pred(c):
                c["status"] = "released"
                c["ended"] = t
                c["note"] = note
                hit.append(c["id"])
        if hit:
            self.promote(t)
        return hit

    def on_release(self, e, t):
        ids = set(e.get("ids") or [])
        res = e.get("resources") or []
        allc = e.get("all", False)

        def pred(c):
            if c["agent"] != e["agent"]:
                return False
            if allc or c["id"] in ids:
                return True
            return any(r in c["resources"] for r in res)
        hit = [c for c in self.claims.values() if c["status"] in ("active", "queued") and pred(c)]
        self._release(pred, t, e.get("note", ""))
        self._stranded(e, t, hit)

    def _stranded(self, e, t, claims):
        um = e.get("unmerged")
        if not um:
            return
        intent = "; ".join(c["intent"] for c in claims if c["intent"]) or e.get("note", "")
        self._unfinished(f"STRANDED-{e['seq']}", e["agent"], t,
                         f"merge {e['agent']}'s unmerged work: {intent}"[:160],
                         f"{e['agent']} left this unmerged ({um.get('reason', '')}). Work: branch "
                         f"{um.get('branch')} @ {str(um.get('head', ''))[:10]}, copy pushed to "
                         f"wip/{e['agent']} (`git fetch origin wip/{e['agent']}`). Files: "
                         f"{', '.join(um.get('files', []))}. Integrate it onto main (or drop the "
                         f"task with a reason).", um.get("files", []))

    def on_msg(self, e, t):
        self.msgs.append(e)

    def on_propose(self, e, t):
        pid = f"P{e['seq']}"
        electorate = [n for n, ag in self.agents.items()
                      if ag.get("role", "agent") == "agent" and self.is_live(n, t)]
        if e["agent"] not in electorate:
            electorate.append(e["agent"])
        self.props[pid] = {
            "id": pid, "agent": e["agent"], "title": e["title"],
            "body": e.get("body", ""), "default": e["default"],
            "deadline": t + e.get("deadline", DEFAULT_DEADLINE),
            "electorate": electorate, "votes": {}, "status": "open",
            "needs_human": e.get("needs_human", False), "created": t,
            "about": e.get("about") or [], "topic": e.get("topic") or "",
            "alts": {},
        }
        if len(electorate) <= 1:
            self.resolve(self.props[pid], t)

    def on_vote(self, e, t):
        p = self.props.get(e["pid"])
        if not p or p["status"] != "open":
            return
        alt = e.get("alt")
        if e["stance"] == "object" and alt:
            alt = self.alt_label(p, alt)
        p["votes"][e["agent"]] = {"stance": e["stance"], "alt": alt,
                                  "reason": e.get("reason", "")}
        self._maybe_resolve_early(p, t)

    def _maybe_resolve_early(self, p, t):
        others = [a for a in p["electorate"] if a != p["agent"]]
        if p["status"] == "open" and all(a in p["votes"] for a in others):
            self.resolve(p, t)

    @staticmethod
    def alt_label(p, alt):
        """Map an alternative to a stable label (A1, A2...) so votes for the
        same alternative add up. Accepts an existing label or identical text."""
        alt = alt.strip()
        if alt in p["alts"] or alt == "REJECT":
            return alt
        for k, v in p["alts"].items():
            if v.strip().lower() == alt.lower():
                return k
        k = f"A{len(p['alts']) + 1}"
        p["alts"][k] = alt
        return k

    def on_withdraw(self, e, t):
        p = self.props.get(e["pid"])
        if p and p["status"] == "open" and p["agent"] == e["agent"]:
            p["status"] = "withdrawn"
            p["resolved"] = t
            self.transitions.append((t, "resolved", p["id"]))

    def on_task_add(self, e, t):
        for spec in e["tasks"]:
            self.tasks[spec["id"]] = {
                "id": spec["id"], "title": spec["title"],
                "files": spec.get("files", []), "deps": spec.get("deps", []),
                "by": e["agent"], "owner": None, "status": "open",
                "note": "", "created": t, "spec": spec.get("spec", ""),
            }

    def on_task_spec(self, e, t):
        task = self.tasks.get(e["tid"])
        if task:
            task["spec"] = (task.get("spec", "") + "\n" + e["spec"]).strip()

    def on_task_take(self, e, t):
        task = self.tasks.get(e["tid"])
        if task and task["status"] in ("open", "taken") and (
                task["owner"] in (None, e["agent"]) or not self.is_live(task["owner"], t)):
            if task["owner"] not in (None, e["agent"]):
                task["took_over_from"] = task["owner"]
            task["owner"] = e["agent"]
            task["status"] = "taken"
            task["offered_to"] = None

    def on_task_done(self, e, t):
        task = self.tasks.get(e["tid"])
        if task:
            task["status"] = "done"
            task["owner"] = task["owner"] or e["agent"]
            task["note"] = e.get("note", "")

    def on_task_drop(self, e, t):
        task = self.tasks.get(e["tid"])
        if task and (task["owner"] == e["agent"] or
                     (task["owner"] is None and task["by"] == e["agent"])):
            task["owner"] = None
            task["status"] = "open"
            task["note"] = e.get("note", "")
            task["offered_to"] = e.get("to")

    def on_policy(self, e, t):
        if e.get("value") in (None, [], ""):
            self.policy.pop(e["key"], None)
        else:
            self.policy[e["key"]] = e["value"]

    def on_review_request(self, e, t):
        rid = f"R{e['seq']}"
        self.reviews[rid] = {"id": rid, "author": e["agent"], "reviewer": e["reviewer"],
                             "ref": e.get("ref", ""), "note": e.get("note", ""),
                             "status": "approved" if e.get("self") else "open",
                             "self": bool(e.get("self")),
                             "verdict_note": "SELF-APPROVED (no live reviewer)" if e.get("self") else "",
                             "created": t,
                             "patch_id": e.get("patch_id"), "head": e.get("head")}

    def on_review_verdict_withdraw(self, e, t):
        r = self.reviews.get(e["rid"])
        if r and r["status"] == "open" and e["agent"] == r["author"]:
            r["status"] = "withdrawn"

    def on_review_reassign(self, e, t):
        r = self.reviews.get(e["rid"])
        if r and r["status"] == "open" and e["agent"] == r["author"]:
            r["previous_reviewers"] = r.get("previous_reviewers", []) + [r["reviewer"]]
            r["reviewer"] = e["reviewer"]
            r["assigned"] = t

    def on_review_verdict(self, e, t):
        r = self.reviews.get(e["rid"])
        if r and r["status"] == "open" and e["agent"] == r["reviewer"]:
            r["status"] = e["verdict"]
            r["verdict_note"] = e.get("note", "")
            r["decided"] = t

    def active_agents(self, exclude=()):
        return [n for n, ag in self.agents.items()
                if ag.get("role", "agent") == "agent" and self.is_live(n) and n not in exclude]

    def owned_by(self, agent):
        """Everything others may be waiting on this agent for."""
        return {
            "claims": [c for c in self.live_claims() if c["agent"] == agent],
            "tasks": [x for x in self.tasks.values() if x["owner"] == agent and x["status"] == "taken"],
            "to_review": [r for r in self.reviews.values() if r["reviewer"] == agent and r["status"] == "open"],
            "my_reviews": [r for r in self.reviews.values() if r["author"] == agent and r["status"] == "open"],
            "proposals": [p for p in self.props.values() if p["agent"] == agent and p["status"] == "open"],
        }

    # ---- queries ---------------------------------------------------------
    def live_claims(self, status=("active", "queued")):
        return [c for c in self.claims.values() if c["status"] in status]

    def waits_for(self):
        g = {}
        for c in self.live_claims(("queued",)):
            for b in c["blocked_by"]:
                holder = self.claims[b]["agent"]
                if holder != c["agent"]:
                    g.setdefault(c["agent"], set()).add(holder)
        return g

    def task_ready(self, task):
        return all(self.tasks.get(d, {}).get("status") == "done" for d in task["deps"])


def replay(events, at=None):
    s = State()
    for e in events:
        s.apply(e)
    s.advance(at if at is not None else now())
    return s


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------

def identity(required=True):
    ident = read_json(os.path.join(state_dir(), "identity.json"), {})
    if os.environ.get("COORD_AGENT"):
        ident["agent"] = os.environ["COORD_AGENT"]
    if required and not ident.get("agent"):
        raise CoordError("no identity: run `coord join --agent NAME --task '...'` first "
                         "(or set COORD_AGENT)")
    return ident


def me():
    return identity()["agent"]


# --------------------------------------------------------------------------
# writing: compare-and-swap append
# --------------------------------------------------------------------------

PROTOCOL_README = """# communication branch

This orphan branch is a coordination bus for AI agents, managed by `coord.py`
(see the `agent-coordination` skill). Do not edit it by hand.

* `events/` - append-only log, one JSON file per event, totally ordered by `seq`.
* `BOARD.md` - human-readable snapshot regenerated on every write.

Every write is a compare-and-swap push of one commit on top of the tip, so the
remote orders all events and locks (claims) can never be double-granted.
"""


def commit_event(build, max_attempts=30):
    """build(state) -> event dict (without seq/t/agent) or raises CoordError.

    Re-runs build() against the freshest state on every retry so that the
    validation that happens inside build() is always against the true tip.
    Returns (event, new_state).
    """
    agent = me()
    for attempt in range(max_attempts):
        tip = fetch()
        events = load_events(tip)
        state = replay(events)
        ev = build(state)
        ev = dict(ev)
        ev["seq"] = state.seq + 1
        ev["t"] = round(now(), 3)
        ev["iso"] = iso(ev["t"])
        ev["agent"] = agent
        ev["v"] = VERSION
        new_state = replay(events + [ev])

        blob = git_out("hash-object", "-w", "--stdin", input=json.dumps(ev, indent=1) + "\n")
        board = git_out("hash-object", "-w", "--stdin", input=render_board(new_state))
        path = f"events/{ev['seq']:07d}-{agent}-{ev['type']}.json"
        fd, idx = tempfile.mkstemp(prefix="coord-index-")
        os.close(fd)
        os.unlink(idx)
        try:
            env = {"GIT_INDEX_FILE": idx}
            if tip:
                git("read-tree", tip, env=env)
            else:
                readme = git_out("hash-object", "-w", "--stdin", input=PROTOCOL_README)
                git("update-index", "--add", "--cacheinfo", f"100644,{readme},README.md", env=env)
            git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
            git("update-index", "--add", "--cacheinfo", f"100644,{board},BOARD.md", env=env)
            tree = git_out("write-tree", env=env)
        finally:
            if os.path.exists(idx):
                os.unlink(idx)
        ident_env = {
            "GIT_AUTHOR_NAME": agent, "GIT_AUTHOR_EMAIL": f"{agent}@coord.local",
            "GIT_COMMITTER_NAME": agent, "GIT_COMMITTER_EMAIL": f"{agent}@coord.local",
        }
        msg = f"[{ev['seq']}] {agent} {ev['type']}: {summarize(ev)}"[:200]
        parents = ["-p", tip] if tip else []
        commit = git_out("commit-tree", tree, *parents, "-m", msg, env=ident_env)
        p = git("push", "-q", "--no-verify", REMOTE, f"{commit}:refs/heads/{BRANCH}", check=False)
        if p.returncode == 0:
            git("update-ref", LOCAL_REF, commit)
            return ev, new_state
        err = p.stderr.lower()
        if not any(k in err for k in ("rejected", "non-fast-forward", "fetch first",
                                      "stale info", "cannot lock ref", "failed to update ref",
                                      "failed to push", "incorrect old value")):
            if attempt >= 4:
                raise CoordError(f"push failed: {p.stderr.strip()}")
        time.sleep(random.uniform(0.05, 0.4) * min(attempt + 1, 6))
    raise CoordError("gave up after too many concurrent-write retries")


def summarize(ev):
    t = ev["type"]
    if t == "msg":
        return f"->{ev.get('to', 'all')}: {ev.get('body', '')}"
    if t == "claim":
        return f"{' '.join(ev['resources'])} ({ev.get('intent', '')})"
    if t == "release":
        what = "all" if ev.get("all") else " ".join(ev.get("ids") or ev.get("resources") or [])
        return f"{what} {ev.get('note', '')}".strip()
    if t == "propose":
        return f"{ev['title']} [default: {ev['default']}]"
    if t == "vote":
        alt = f" -> {ev['alt']}" if ev.get("alt") else ""
        return f"{ev['pid']} {ev['stance']}{alt} {ev.get('reason', '')}".strip()
    if t == "join":
        return ev.get("task", "")
    if t.startswith("task_"):
        if t == "task_add":
            return ", ".join(f"{s['id']}:{s['title']}" for s in ev["tasks"])
        return f"{ev['tid']} {ev.get('note', '')}".strip()
    if t == "status":
        return ev.get("note", "")
    return ev.get("note", "")


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render_board(s):
    t = s.clock
    out = [f"# Coordination board", "",
           f"_Snapshot at seq {s.seq}, {iso(t)}. Generated by coord.py; do not edit._", ""]
    out += ["## Agents", "", "| agent | human | status | task | branch | last seen |",
            "|---|---|---|---|---|---|"]
    for ag in s.agents.values():
        st = ag["status"]
        if st == "active" and not s.is_live(ag["name"]):
            st = "offline" if ag.get("offline") else "stale"
        out.append(f"| {ag['name']} | {ag['human']} | {st} | {ag['task']} | "
                   f"{ag['branch']} | {iso(ag['last_seen'])} |")
    out += ["", "## Claims (locks)", ""]
    live = s.live_claims()
    if not live:
        out.append("_none_")
    for c in live:
        extra = f" waiting on {', '.join(c['blocked_by'])}" if c["status"] == "queued" else \
            f" expires {iso(c['expires'])}"
        out.append(f"- **{c['id']}** `{c['status']}` {c['agent']}: "
                   f"{', '.join('`%s`' % r for r in c['resources'])} ({c['mode']}) "
                   f"- {c['intent']}{extra}")
    out += ["", "## Tasks", ""]
    if not s.tasks:
        out.append("_none_")
    for task in s.tasks.values():
        deps = f" deps:{','.join(task['deps'])}" if task["deps"] else ""
        out.append(f"- [{'x' if task['status'] == 'done' else ' '}] **{task['id']}** "
                   f"{task['title']} - {task['status']}"
                   f"{' by ' + task['owner'] if task['owner'] else ''}{deps}"
                   f"{' - ' + task['note'] if task['note'] else ''}")
    out += ["", "## Proposals & decisions", ""]
    if not s.props:
        out.append("_none_")
    for p in reversed(list(s.props.values())):
        flag = " **NEEDS HUMAN**" if p["needs_human"] else ""
        if p["status"] == "open":
            out.append(f"- **{p['id']}** OPEN (deadline {iso(p['deadline'])}) {p['agent']}: "
                       f"{p['title']} - default: {p['default']}{flag}")
        else:
            out.append(f"- **{p['id']}** {p['status'].upper()}: {p['title']} -> "
                       f"**{p.get('outcome', '-')}**{flag}")
    out += ["", "## Recent messages", ""]
    for m in s.msgs[-20:]:
        out.append(f"- #{m['seq']} {iso(m['t'])} **{m['agent']}** -> {m.get('to', 'all')}"
                   f"{' [' + m['kind'] + ']' if m.get('kind') else ''}: {m.get('body', '')}")
    return "\n".join(out) + "\n"


def fmt_claim(c, s):
    base = f"{c['id']} {c['status']} {c['agent']} {' '.join(c['resources'])}"
    if c["mode"] == "shared":
        base += " (shared)"
    if c["intent"]:
        base += f" - {c['intent']}"
    if c["status"] == "queued" and c["blocked_by"]:
        held = sorted({f"{b}/{s.claims[b]['agent']}" for b in c["blocked_by"]
                       if s.claims[b]["status"] == "active"})
        ahead = [b for b in c["blocked_by"] if s.claims[b]["status"] == "queued"]
        base += f" [queue position {len(ahead) + 1}; held by {', '.join(held) or '-'}]"
    elif c["status"] == "active":
        base += f" [expires {ago(c['expires'])}]"
    return base


def fmt_event(e):
    return f"#{e['seq']} {e['agent']} {e['type']}: {summarize(e)}"


# --------------------------------------------------------------------------
# inbox / notifications
# --------------------------------------------------------------------------

def cursor_path(agent):
    return os.path.join(state_dir(), f"cursor-{agent}.json")


def compute_inbox(s, agent, cur):
    """Return list of notification lines relevant to `agent` since cursor."""
    lines = []
    last = cur.get("seq", 0)
    my_live = {c["id"] for c in s.live_claims() if c["agent"] == agent}
    for e in s.events:
        if e["seq"] <= last or e["agent"] == agent:
            continue
        typ = e["type"]
        if typ == "msg":
            to = e.get("to", "all")
            targets = {x.strip() for x in to.split(",")}
            if "all" in targets or agent in targets:
                kind = f"[{e['kind']}] " if e.get("kind") else ""
                re_ = f" (re #{e['re']})" if e.get("re") else ""
                tag = "INCIDENT" if e.get("kind") == "incident" else "MSG"
                role = s.agents.get(e["agent"], {}).get("role", "agent")
                who = f"{e['agent']} (human)" if role == "human" else e["agent"]
                lines.append(f"#{e['seq']} {tag} {who}->{to}{re_}: {kind}{e.get('body', '')}")
        elif typ == "propose":
            pid = f"P{e['seq']}"
            p = s.props.get(pid)
            if p and p["status"] == "open":
                lines.append(f"#{e['seq']} PROPOSAL {pid} by {e['agent']}: {e['title']} | "
                             f"default: {e['default']} | {e.get('body', '')} | "
                             f"closes {ago(p['deadline'])}{alts_text(p)}. Silence = accept; "
                             f"to object: coord vote {pid} object --alt A<n>|'...' --reason '...'")
        elif typ == "vote":
            p = s.props.get(e["pid"])
            if p and p["agent"] == agent:
                v = p["votes"].get(e["agent"], {})
                alt = v.get("alt")
                alt = f" -> {alt}={p['alts'].get(alt, alt)}" if alt else ""
                lines.append(f"#{e['seq']} VOTE on your {e['pid']}: {e['agent']} {e['stance']}"
                             f"{alt} {e.get('reason', '')}")
        elif typ == "claim":
            c = s.claims.get(f"C{e['seq']}")
            if c and c["status"] == "queued" and set(c["blocked_by"]) & my_live:
                if c.get("urgent"):
                    lines.append(f"#{e['seq']} URGENT WAITING ON YOU: {e['agent']} needs "
                                 f"{' '.join(c['resources'])} ({c['intent']}). Commit/stash, "
                                 f"release at your next safe point, re-claim afterwards.")
                else:
                    lines.append(f"#{e['seq']} WAITING ON YOU: {e['agent']} queued for "
                                 f"{' '.join(c['resources'])} ({c['intent']}). Release as soon "
                                 f"as your change is integrated.")
        elif typ == "review_request":
            r = s.reviews.get(f"R{e['seq']}")
            if r and r["reviewer"] == agent:
                spec = s.tasks.get(e.get("task") or "", {}).get("spec", "")
                spec = f" ACCEPTANCE CRITERIA ({e['task']}): {spec} |" if spec else ""
                lines.append(f"#{e['seq']} REVIEW REQUEST {r['id']} from {e['agent']}: "
                             f"{r['ref']} - {r['note']} [status {r['status']}].{spec} "
                             f"Review against the criteria, not the description. Respond with "
                             f"`coord review approve {r['id']} -m ..` or `coord review changes "
                             f"{r['id']} -m '<concrete required change>'`.")
        elif typ == "review_verdict":
            r = s.reviews.get(e["rid"])
            if r and r["author"] == agent:
                lines.append(f"#{e['seq']} REVIEW {r['id']} {e['verdict'].upper()} by "
                             f"{e['agent']}: {e.get('note', '')}")
        elif typ == "task_drop" and e.get("to") in (agent, "any"):
            t_ = s.tasks.get(e["tid"], {})
            if t_.get("status") == "open":
                lines.append(f"#{e['seq']} HANDOFF {e['tid']} ({t_.get('title', '')}) from "
                             f"{e['agent']} to {e['to']}: {e.get('note', '')}. Take it with "
                             f"`coord task take {e['tid']}`.")
        elif typ in ("join", "leave"):
            lines.append(f"#{e['seq']} {e['agent']} {typ}: {e.get('task', e.get('note', ''))}")
        elif typ == "adopt":
            lines.append(f"#{e['seq']} {e['agent']} ADOPTED all work of {e['from']} "
                         f"(claims, tasks, reviews): {e.get('note', '')}")
        elif typ == "task_take":
            t_ = s.tasks.get(e["tid"], {})
            if t_.get("took_over_from") and t_.get("owner") == e["agent"]:
                lines.append(f"#{e['seq']} TAKEOVER {e['tid']}: {e['agent']} took it from stale "
                             f"agent {t_['took_over_from']}")
        elif typ == "task_add":
            lines.append(f"#{e['seq']} TASKS by {e['agent']}: " +
                         "; ".join(f"{x['id']} {x['title']}" for x in e["tasks"]))
        elif typ == "release":
            pass  # surfaced via GRANTED below when it matters
    # derived: claim state changes and decisions
    seen_active = set(cur.get("active", []))
    seen_queued = set(cur.get("queued", []))
    for c in s.claims.values():
        if c["agent"] != agent:
            continue
        if c["status"] == "active" and c["id"] in seen_queued:
            lines.append(f"GRANTED {c['id']}: you now hold {' '.join(c['resources'])}. "
                         f"Sync with the integration branch (git pull --rebase) BEFORE editing.")
        if c["status"] == "expired" and (c["id"] in seen_active or c["id"] in seen_queued):
            lines.append(f"EXPIRED {c['id']}: your lease on {' '.join(c['resources'])} lapsed; "
                         f"re-claim before editing.")
    for r in s.reviews.values():
        if r["author"] == agent and r["status"] == "open" and not s.is_live(r["reviewer"]) \
                and r["id"] not in cur.get("stale_reviews", []):
            lines.append(f"REVIEWER STALE {r['id']}: {r['reviewer']} is not live; run "
                         f"`coord review reassign {r['id']}`.")
    for e in s.events:
        if e["seq"] > last and e["type"] == "review_reassign" and e.get("reviewer") == agent:
            r = s.reviews.get(e["rid"], {})
            lines.append(f"#{e['seq']} REVIEW REQUEST {e['rid']} (reassigned to you) from "
                         f"{r.get('author')}: {r.get('ref')} - {r.get('note')}. "
                         f"`coord review approve|changes {e['rid']} -m ..`")
    def vt(v):
        try:
            return tuple(int(x) for x in str(v).split("."))
        except ValueError:
            return (0,)
    newest = max((e.get("v", "0") for e in s.events if e["agent"] != agent), key=vt, default="0")
    if vt(newest) > vt(VERSION) and cur.get("warned_v") != newest:
        lines.append(f"TOOL OUTDATED: other agents run coord {newest}, you run {VERSION}. "
                     f"Update the skill (git pull the branch that carries "
                     f".claude/skills/agent-coordination) and re-read SKILL.md.")
    for t in s.tasks.values():
        if t.get("unfinished") and t["status"] == "open" and t["id"] not in cur.get("unfinished", []) \
                and t["by"] != agent:
            lines.append(f"UNFINISHED {t['id']}: {t['title']} - `coord task take {t['id']}` "
                         f"shows how to recover it.")
    seen_res = set(cur.get("resolved", []))
    for p in s.props.values():
        if p["status"] != "open" and p["id"] not in seen_res:
            lines.append(f"DECIDED {p['id']} ({p['title']}): {p['status']} -> {p.get('outcome', '-')}")
    return lines


def snapshot_cursor(s, agent):
    return {
        "seq": s.seq,
        "active": [c["id"] for c in s.claims.values() if c["agent"] == agent and c["status"] == "active"],
        "queued": [c["id"] for c in s.claims.values() if c["agent"] == agent and c["status"] == "queued"],
        "resolved": [p["id"] for p in s.props.values() if p["status"] != "open"],
        "open": [p["id"] for p in s.props.values() if p["status"] == "open"],
        "since": s.clock,
        "warned_v": max((e.get("v", "0") for e in s.events if e["agent"] != agent),
                        default="0"),
        "unfinished": [t["id"] for t in s.tasks.values() if t.get("unfinished")],
        "stale_reviews": [r["id"] for r in s.reviews.values() if r["author"] == agent
                          and r["status"] == "open" and not s.is_live(r["reviewer"])],
    }


def read_inbox(s, agent, mark=True):
    cp = cursor_path(agent)
    cur = read_json(cp, {})
    lines = compute_inbox(s, agent, cur)
    if mark:
        write_json(cp, snapshot_cursor(s, agent))
    return lines


def mark_seen(s, agent, ev=None):
    """After our own write: record claims we just created so they are not
    reported back to us, but leave everything else (others' messages,
    grants, decisions) unread."""
    cp = cursor_path(agent)
    cur = read_json(cp, {})
    if ev and ev["type"] == "claim":
        c = s.claims.get(f"C{ev['seq']}")
        if c:
            key = "active" if c["status"] == "active" else "queued"
            cur[key] = sorted(set(cur.get(key, [])) | {c["id"]})
    if ev and ev["type"] in ("propose", "vote"):
        pid = ev.get("pid") or f"P{ev['seq']}"
        p = s.props.get(pid)
        if p and p["status"] != "open":
            cur["resolved"] = sorted(set(cur.get("resolved", [])) | {pid})
    write_json(cp, cur)


def pending_tail(s, agent):
    """Short unread hint appended to write commands."""
    cur = read_json(cursor_path(agent), {})
    n = len(compute_inbox(s, agent, cur))
    return f"({n} unread - run `coord inbox`)" if n else ""


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def current_branch():
    p = git("rev-parse", "--abbrev-ref", "HEAD", check=False)
    return p.stdout.strip()


def cmd_join(a):
    ident = identity(required=False)
    agent = a.agent or ident.get("agent")
    if not agent:
        raise CoordError("--agent NAME is required the first time")
    if not all(ch.isalnum() or ch in "-_." for ch in agent):
        raise CoordError("agent names may only contain letters, digits, - _ .")
    ident.update({"agent": agent})
    if a.human:
        ident["human"] = a.human
    session = getattr(a, "session", None) or os.environ.get("CLAUDE_SESSION_ID") or ""
    if session:
        ident["session"] = session
    write_json(os.path.join(state_dir(), "identity.json"), ident)
    os.environ["COORD_AGENT"] = agent
    resumed = {}

    def build(s):
        resumed["prev"] = s.agents.get(agent)
        ev = {"type": "join", "human": a.human or ident.get("human", ""),
              "task": a.task or "", "branch": a.branch or current_branch(),
              "role": a.role}
        if session:
            ev["session"] = session
        return ev
    ev, s = commit_event(build)
    if resumed.get("prev"):
        print(f"resumed identity {agent} (new session; previous activity "
              f"{ago(resumed['prev']['last_seen'])}). Everything it owned is still yours.")
    # first join: treat everything already on the bus as read except open items
    cp = cursor_path(agent)
    if not os.path.exists(cp):
        snap = snapshot_cursor(s, agent)
        snap["seq"] = 0 if a.history else s.seq
        write_json(cp, snap)
    print(f"joined as {agent} (seq {ev['seq']}).")
    if not a.no_hooks and a.role == "agent":
        try:
            msg = install_guard_hook()
            if msg:
                print(msg)
        except CoordError as e:
            print(f"note: pre-commit guard not installed ({e})")
    print_brief(s, agent)


def print_brief(s, agent):
    if s.policy:
        print("team policy: " + "; ".join(f"{k}={v}" for k, v in s.policy.items()) +
              " (pushes are checked by the pre-push guard)")
    others = [ag for ag in s.agents.values() if ag["name"] != agent and s.is_live(ag["name"])]
    stale = [ag for ag in s.agents.values() if ag["name"] != agent and ag["status"] == "active"
             and not s.is_live(ag["name"]) and any(s.owned_by(ag["name"]).values())]
    for ag in stale:
        o = s.owned_by(ag["name"])
        print(f"STALE agent {ag['name']} (human {ag['human'] or '-'}, last seen "
              f"{ago(ag['last_seen'])}) still owns: "
              + ", ".join(f"{k}={len(v)}" for k, v in o.items() if v)
              + ". Its locks lapse by lease; its tasks can be taken over (`task take`); "
                "the same human's new session can `coord adopt " + ag["name"] + "`.")
    mine = s.owned_by(agent)
    if any(mine.values()):
        print("YOU OWN (resume this work or hand it off):")
        for c in mine["claims"]:
            print("  claim " + fmt_claim(c, s))
        for x in mine["tasks"]:
            print(f"  task {x['id']}: {x['title']}{' - ' + x['note'] if x['note'] else ''}")
        for r in mine["to_review"]:
            print(f"  review to do {r['id']} from {r['author']}: {r['ref']} - {r['note']}")
        for r in mine["my_reviews"]:
            print(f"  your review request {r['id']} with {r['reviewer']} ({r['status']})")
        for p in mine["proposals"]:
            print(f"  your open proposal {p['id']}: {p['title']}")
    if others:
        print("active agents: " + "; ".join(f"{o['name']} ({o['task']})" for o in others))
    live = s.live_claims()
    if live:
        print("claims:")
        for c in live:
            print("  " + fmt_claim(c, s))
    openp = [p for p in s.props.values() if p["status"] == "open"]
    for p in openp:
        print(f"open proposal {p['id']} by {p['agent']}: {p['title']} (default: {p['default']}, "
              f"closes {ago(p['deadline'])})")
    unf = [t for t in s.tasks.values() if t.get("unfinished") and t["status"] != "done"]
    if unf:
        print("UNFINISHED WORK LEFT BY OTHERS (take it before starting similar work):")
        for t in unf:
            print(f"  {t['id']} [{t['status']}{' ' + t['owner'] if t['owner'] else ''}] {t['title']}")
    open_tasks = [t for t in s.tasks.values() if t["status"] != "done" and not t.get("unfinished")]
    if open_tasks:
        print("tasks:")
        for t in open_tasks:
            ready = "" if s.task_ready(t) else " (blocked by deps)"
            print(f"  {t['id']} {t['status']}{' by ' + t['owner'] if t['owner'] else ''}: "
                  f"{t['title']}{ready}")


def unmerged_files():
    """Files changed locally (commits not on REMOTE/main, or uncommitted edits)."""
    if git("fetch", "-q", "--no-tags", REMOTE, "main", check=False).returncode != 0:
        return [], None
    base = f"{REMOTE}/main"
    if not git("rev-parse", "--verify", "-q", base, check=False).stdout.strip():
        return [], None
    files = set(git("log", f"{base}..HEAD", "--name-only", "--format=", check=False)
                .stdout.split())
    files |= set(git("diff", "--name-only", "HEAD", check=False).stdout.split())
    files |= set(git("ls-files", "--others", "--exclude-standard", check=False).stdout.split())
    return sorted(f for f in files if f), git("rev-parse", "HEAD", check=False).stdout.strip()


def autosave(agent, force=False):
    """Push unpushed commits AND uncommitted edits to wip/<agent> without touching the
    working tree or index, so a dead laptop or a usage limit loses at most minutes."""
    try:
        head = git("rev-parse", "--verify", "-q", "HEAD", check=False).stdout.strip()
        if not head:
            return None
        dirty = git("status", "--porcelain", check=False).stdout.strip()
        unpushed = git("rev-list", "--count", "HEAD", "--not", "--remotes",
                       check=False).stdout.strip()
        if not dirty and unpushed in ("", "0"):
            return None
        sha = head
        if dirty:
            fd, idx = tempfile.mkstemp(prefix="coord-autosave-")
            os.close(fd)
            os.unlink(idx)
            env = {"GIT_INDEX_FILE": idx}
            try:
                git("read-tree", "HEAD", env=env)
                git("add", "-A", env=env)
                tree = git_out("write-tree", env=env)
            finally:
                if os.path.exists(idx):
                    os.unlink(idx)
            ienv = {"GIT_AUTHOR_NAME": agent, "GIT_AUTHOR_EMAIL": f"{agent}@coord.local",
                    "GIT_COMMITTER_NAME": agent, "GIT_COMMITTER_EMAIL": f"{agent}@coord.local"}
            sha = git_out("commit-tree", tree, "-p", head, "-m",
                          f"coord autosave ({agent}): uncommitted work", env=ienv)
        stamp = os.path.join(state_dir(), "autosave-last")
        if not force and read_json(stamp, {}).get("sha") == sha:
            return sha
        p = git("push", "-q", "--no-verify", "-f", REMOTE, f"{sha}:refs/heads/wip/{agent}",
                check=False)
        if p.returncode == 0:
            write_json(stamp, {"sha": sha, "t": now()})
            return sha
    except Exception:
        pass
    return None


def guard_unmerged(s, agent, claims, a, verb):
    """Refuse to release/leave while claimed work is not on main, unless declared."""
    files, head = unmerged_files()
    paths = {split_res(r)[0] for c in claims for r in c["resources"]}
    hit = [f for f in files if any(paths_overlap(p, f) for p in paths)] if paths else files
    if not hit:
        return None
    if not a.unmerged:
        raise CoordError(
            f"NOT ON MAIN: {', '.join(hit[:8])} changed here but not on {REMOTE}/main. Done "
            f"means on main: `git pull --rebase {REMOTE} main && git push {REMOTE} HEAD:main`, "
            f"then {verb} again. If it truly must stay unmerged (review pending, abandoning), "
            f"`coord {verb} --unmerged '<reason>'` - it becomes a STRANDED task for the team.")
    autosave(agent, force=True)
    return {"reason": a.unmerged, "branch": current_branch(), "head": head,
            "files": hit, "wip": f"wip/{agent}"}


def cmd_leave(a):
    agent = me()
    s0 = replay(load_events(fetch()))
    um = guard_unmerged(s0, agent, [c for c in s0.live_claims() if c["agent"] == agent], a, "leave")

    def build(s):
        ev = {"type": "leave", "note": a.note or ""}
        if um:
            ev["unmerged"] = um
        return ev
    ev, s = commit_event(build)
    print(f"left (seq {ev['seq']}); all your claims were released."
          + (f" Unmerged work saved to wip/{agent} and listed as STRANDED-{ev['seq']}." if um else ""))


def cmd_status(a):
    ev, s = commit_event(lambda s: {"type": "status", "note": a.note, "task": a.task or ""})
    mark_seen(s, me(), ev)
    print(f"status posted (#{ev['seq']}). {pending_tail(s, me())}")


def cmd_say(a):
    body = " ".join(a.text)
    if not body.strip():
        raise CoordError("empty message")

    def build(s):
        if a.to != "all":
            for t in a.to.split(","):
                if t.strip() not in s.agents:
                    raise CoordError(f"unknown agent '{t.strip()}' (known: {', '.join(s.agents)})")
        ev = {"type": "msg", "to": a.to, "body": body}
        if a.kind:
            ev["kind"] = a.kind
        if a.re:
            ev["re"] = a.re
        if a.needs_human:
            ev["needs_human"] = True
        return ev
    ev, s = commit_event(build)
    mark_seen(s, me(), ev)
    print(f"sent #{ev['seq']}. {pending_tail(s, me())}")
    if a.to != "all":
        for t in a.to.split(","):
            t = t.strip()
            ag = s.agents.get(t, {})
            if not s.is_live(t):
                state = "has LEFT" if ag.get("status") == "left" else "is not live (stale/offline)"
                orphan = [x["id"] for x in s.tasks.values() if x["status"] != "done" and
                          (x["owner"] == t or (x["owner"] is None and x["by"] == t))]
                print(f"warning: {t} {state}; nobody may read this."
                      + (f" Tasks it left behind: {', '.join(orphan)} - take them yourself "
                         f"(`coord task take ID`) or hand them on." if orphan else ""))


def cmd_claim(a):
    agent = me()
    resources = [r for r in a.resources]
    ttl = parse_dur(a.ttl) if a.ttl else DEFAULT_TTL
    mode = "shared" if a.shared else "exclusive"

    def build(s):
        fake = {"type": "claim", "resources": resources, "mode": mode, "intent": "",
                "ttl": ttl, "seq": s.seq + 1, "t": now(), "agent": agent,
                "urgent": bool(a.urgent)}
        sim = replay(s.events + [fake])
        c = sim.claims[f"C{s.seq + 1}"]
        if c["status"] == "queued":
            g = sim.waits_for()
            stack, seen = list(g.get(agent, ())), set()
            while stack:
                x = stack.pop()
                if x == agent:
                    mine = [m["id"] for m in s.live_claims(("active",)) if m["agent"] == agent]
                    raise CoordError(
                        f"DEADLOCK: granting this would require waiting on someone who is "
                        f"(transitively) waiting on you. Integrate and release your claims "
                        f"({', '.join(mine)}) first, or release them and claim everything you "
                        f"need in one `coord claim A B ...` call.")
                if x in seen:
                    continue
                seen.add(x)
                stack.extend(g.get(x, ()))
            if a.no_queue:
                raise CoordError("BUSY (not queued): " + fmt_claim(c, sim))
        ev = {"type": "claim", "resources": resources, "mode": mode,
              "intent": a.intent or "", "ttl": ttl}
        if a.urgent:
            ev["urgent"] = True
        return ev
    ev, s = commit_event(build)
    c = s.claims[f"C{ev['seq']}"]
    mark_seen(s, agent, ev)
    recent = [o for o in s.claims.values() if o["agent"] != agent and o["status"] in
              ("released", "expired") and s.clock - o.get("ended", 0) < 2 * 3600 and
              any(res_overlap(r1, r2) for r1 in resources for r2 in o["resources"])]
    if recent:
        print("RECENT WORK on these files by others (check it does not contradict yours; if it "
              "does, build a design that satisfies both instead of overwriting):")
        for o in recent[-6:]:
            print(f"  {o['agent']} ({ago(o.get('ended', o['created']))}): {o['intent']}"
                  f"{' -> ' + o['note'] if o.get('note') else ''}")
    if c["status"] == "active":
        print(f"GRANTED {c['id']}: {' '.join(resources)} (lease {fmt_dur(ttl)}, renewed by any "
              f"coord activity). Sync your branch with the integration branch, then edit.")
    else:
        print(f"QUEUED {c['id']}: {fmt_claim(c, s)}")
        pairs = []
        for b in c["blocked_by"]:
            o = s.claims[b]
            for r1 in c["resources"]:
                for r2 in o["resources"]:
                    if res_overlap(r1, r2):
                        pairs.append(f"{r1} <-> {r2} ({o['id']}/{o['agent']}, {o['status']})")
        if pairs:
            print("conflicts: " + "; ".join(sorted(set(pairs))))
            free = [r for r in c["resources"] if not any(p.startswith(r + " ") for p in pairs)]
            if free:
                print(f"not contended: {' '.join(free)} - if you can start with those, "
                      f"release {c['id']} and claim them separately.")
            print("If you only ADD new lines to a contended file (new tests, a changelog "
                  "line, a new config constant), claim it with --shared instead.")
        print(f"Do other work or run `coord wait --claim {c['id']}`. Do NOT edit these "
              f"resources until granted.")
    tail = pending_tail(s, agent)
    if tail:
        print(tail)


def cmd_release(a):
    agent = me()
    before = {}
    s0 = replay(load_events(fetch()))
    mine0 = [c for c in s0.live_claims() if c["agent"] == agent]
    if a.what and not a.all:
        mine0 = [c for c in mine0 if c["id"] in a.what or any(r in c["resources"] for r in a.what)]
    um = guard_unmerged(s0, agent, mine0, a, "release")

    def build(s):
        before.clear()
        before.update({c["id"]: c["status"] for c in s.claims.values()})
        mine = [c for c in s.live_claims() if c["agent"] == agent]
        if not mine:
            raise CoordError("you hold no live claims")
        ev = {"type": "release", "note": a.note or ""}
        if um:
            ev["unmerged"] = um
        if a.all or not a.what:
            ev["all"] = True
        else:
            ids = [w for w in a.what if w in s.claims]
            res = [w for w in a.what if w not in s.claims]
            ev["ids"], ev["resources"] = ids, res
        return ev
    ev, s = commit_event(build)
    mark_seen(s, agent, ev)
    gone = [cid for cid, st in before.items() if st in ("active", "queued")
            and s.claims[cid]["agent"] == agent and s.claims[cid]["status"] == "released"]
    print(f"released {', '.join(gone) or '-'} (#{ev['seq']}).")
    for cid, st in before.items():
        c = s.claims[cid]
        if st == "queued" and c["status"] == "active" and c["agent"] != agent:
            print(f"  next holder: {c['agent']} got {cid} {' '.join(c['resources'])}")
    tail = pending_tail(s, agent)
    if tail:
        print(tail)


def cmd_renew(a):
    ev, s = commit_event(lambda s: {"type": "renew"})
    mark_seen(s, me(), ev)
    print(f"leases renewed (#{ev['seq']}).")


def cmd_propose(a):
    dl = parse_dur(a.deadline) if a.deadline else DEFAULT_DEADLINE

    about = list(a.about or [])
    topic = (a.topic or "").strip().lower()
    if not about and not topic:
        raise CoordError("say what the decision governs so duplicates can be detected: "
                         "--about <resource, e.g. store/cart.py#total> and/or --topic <slug>")

    def build(s):
        for p in s.props.values():
            same = (topic and p["topic"] == topic) or \
                any(res_overlap(x, y) for x in about for y in p["about"])
            similar = not same and p["status"] == "open" and \
                title_similarity(a.title, p["title"]) >= 0.5
            if not (same or similar):
                continue
            if p["status"] == "open":
                if a.force and similar:
                    continue
                raise CoordError(
                    f"DUPLICATE: {p['id']} by {p['agent']} is already open on this"
                    f"{' (similar title)' if similar else ''}: \"{p['title']}\" "
                    f"default: {p['default']}" + alts_text(p) + "\n"
                    f"Do not open a competing proposal. Either `coord vote {p['id']} accept`, "
                    f"or `coord vote {p['id']} object --alt A<n>|'<your alternative>'`."
                    + (" (--force if it really is a different question)" if similar else ""))
            if p["status"] == "decided" and same and not a.reopen:
                raise CoordError(
                    f"ALREADY DECIDED: {p['id']} \"{p['title']}\" -> {p['outcome']}. Follow it. "
                    f"Only if it is impossible to implement, re-run with --reopen and say why in --body.")
        return {"type": "propose", "title": a.title, "body": a.body or "",
                "default": a.default, "deadline": dl, "needs_human": a.needs_human,
                "about": about, "topic": topic}
    ev, s = commit_event(build)
    mark_seen(s, me(), ev)
    p = s.props[f"P{ev['seq']}"]
    if p["status"] != "open":
        print(f"{p['id']} decided immediately (no other active agents): {p['outcome']}")
    else:
        print(f"{p['id']} open until {iso(p['deadline'])} ({fmt_dur(dl)}). Electorate: "
              f"{', '.join(p['electorate'])}. Silence = accept; resolves early once everyone "
              f"voted. Proceed on the default unless it is hard to undo; "
              f"`coord wait --proposal {p['id']}` to block; `coord withdraw {p['id']}` to retract.")


def title_similarity(x, y):
    stop = {"the", "a", "an", "of", "for", "in", "on", "to", "and", "or", "with", "is", "use"}
    wx = {w.strip("().,:;?!'\"").lower() for w in x.split()} - stop
    wy = {w.strip("().,:;?!'\"").lower() for w in y.split()} - stop
    wx.discard(""); wy.discard("")
    if not wx or not wy:
        return 0.0
    return len(wx & wy) / len(wx | wy)


def alts_text(p):
    if not p.get("alts"):
        return ""
    counts = {}
    for v in p["votes"].values():
        if v.get("alt"):
            counts[v["alt"]] = counts.get(v["alt"], 0) + 1
    return " | alternatives: " + "; ".join(
        f"{k}={txt} ({counts.get(k, 0)} vote{'s' if counts.get(k, 0) != 1 else ''})"
        for k, txt in p["alts"].items())


def cmd_vote(a):
    def build(s):
        p = s.props.get(a.pid)
        if not p:
            raise CoordError(f"no proposal {a.pid}")
        if p["status"] != "open":
            raise CoordError(f"{a.pid} already {p['status']} -> {p.get('outcome')}")
        if a.stance == "object" and not a.alt:
            raise CoordError("an objection must name a concrete alternative: --alt '...'"
                             " (use --alt REJECT to argue for doing nothing)" + alts_text(p))
        if a.alt and a.alt.strip().upper().startswith("A") and a.alt.strip()[1:].isdigit() \
                and a.alt.strip() not in p["alts"]:
            raise CoordError(f"{p['id']} has no alternative {a.alt}" + alts_text(p))
        if p["agent"] == me():
            raise CoordError("you proposed this; your vote is the default. "
                             f"Use `coord withdraw {p['id']}` to retract it.")
        return {"type": "vote", "pid": a.pid, "stance": a.stance,
                "alt": a.alt, "reason": a.reason or ""}
    ev, s = commit_event(build)
    mark_seen(s, me(), ev)
    p = s.props[a.pid]
    extra = f" -> resolved: {p['status']} '{p['outcome']}'" if p["status"] != "open" else \
        f" ({len(p['votes'])}/{len(p['electorate']) - 1} voted, closes {ago(p['deadline'])})"
    print(f"voted (#{ev['seq']}){extra}.{alts_text(p)}")


def cmd_withdraw(a):
    def build(s):
        p = s.props.get(a.pid)
        if not p or p["agent"] != me() or p["status"] != "open":
            raise CoordError("can only withdraw your own open proposal")
        return {"type": "withdraw", "pid": a.pid}
    commit_event(build)
    print("withdrawn.")


def cmd_task(a):
    agent = me()
    if a.task_cmd == "add":
        specs = []
        for item in a.items:
            # "ID: title [files=a,b] [deps=T1,T2]"
            if ":" not in item:
                raise CoordError(f"task spec must be 'ID: title', got {item!r}")
            tid, rest = item.split(":", 1)
            files, deps, words = [], [], []
            for w in rest.split():
                if w.startswith("files="):
                    files = [x for x in w[6:].split(",") if x]
                elif w.startswith("deps="):
                    deps = [x for x in w[5:].split(",") if x]
                else:
                    words.append(w)
            specs.append({"id": tid.strip(), "title": " ".join(words), "files": files, "deps": deps,
                          "spec": a.spec or ""})

        def build(s):
            ids = {x["id"] for x in specs} | set(s.tasks)
            bad = sorted({d for x in specs for d in x["deps"] if d not in ids})
            if bad:
                raise CoordError(f"deps must be task ids; unknown: {', '.join(bad)} "
                                 f"(to wait on a person's work, add a task for it first)")
            dup = [x["id"] for x in specs if x["id"] in s.tasks]
            if dup:
                raise CoordError(f"task ids already exist: {', '.join(dup)} - "
                                 f"someone already planned this; read `coord tasks`")
            return {"type": "task_add", "tasks": specs}
        ev, s = commit_event(build)
        print(f"added {len(specs)} task(s) (#{ev['seq']}).")
    elif a.task_cmd == "take":
        def build(s):
            t = s.tasks.get(a.tid)
            if not t:
                raise CoordError(f"no task {a.tid}")
            if t["owner"] and t["owner"] != agent and s.is_live(t["owner"]):
                raise CoordError(f"TAKEN: {a.tid} is owned by {t['owner']} (live); pick another")
            if t["status"] == "done":
                raise CoordError(f"{a.tid} is already done")
            pending = [d for d in t["deps"] if s.tasks.get(d, {}).get("status") != "done"]
            if pending and not a.force:
                raise CoordError(f"BLOCKED: {a.tid} depends on unfinished {', '.join(pending)}; "
                                 f"`coord wait --task {pending[0]}` (or --force to start early "
                                 f"against a published contract)")
            return {"type": "task_take", "tid": a.tid}
        ev, s = commit_event(build)
        t = s.tasks[a.tid]
        if t.get("spec"):
            print(f"ACCEPTANCE CRITERIA for {a.tid} (verbatim - build and test against THIS, "
                  f"not a summary):\n{t['spec']}")
        print(f"you own {a.tid}: {t['title']}" +
              (f" (files: {', '.join(t['files'])})" if t["files"] else "") +
              ("" if s.task_ready(t) else " WARNING: deps not done yet"))
    elif a.task_cmd == "spec":
        def build(s):
            if a.tid not in s.tasks:
                raise CoordError(f"no task {a.tid}")
            return {"type": "task_spec", "tid": a.tid, "spec": a.text}
        ev, s = commit_event(build)
        print(f"spec recorded on {a.tid} (#{ev['seq']}).")
    elif a.task_cmd in ("done", "drop"):
        def build(s):
            if a.tid not in s.tasks:
                raise CoordError(f"no task {a.tid}")
            ev = {"type": "task_" + a.task_cmd, "tid": a.tid, "note": a.note or ""}
            if a.task_cmd == "drop":
                tk = s.tasks[a.tid]
                if tk["owner"] != agent and not (tk["owner"] is None and tk["by"] == agent):
                    raise CoordError(f"you do not own {a.tid}")
                if getattr(a, "to", None):
                    if a.to != "any" and a.to not in s.agents:
                        raise CoordError(f"unknown agent {a.to}")
                    ev["to"] = a.to
            return ev
        ev, s = commit_event(build)
        print(f"{a.tid} {a.task_cmd} (#{ev['seq']}).")
        if a.task_cmd == "done":
            ready = [t for t in s.tasks.values() if t["status"] == "open" and s.task_ready(t)
                     and a.tid in t["deps"]]
            for t in ready:
                print(f"  unblocked: {t['id']} {t['title']}")
    else:
        s = replay(load_events(fetch()))
        if not s.tasks:
            print("no tasks")
        for t in s.tasks.values():
            ready = "" if s.task_ready(t) or t["status"] == "done" else " (deps pending)"
            print(f"{t['id']} [{t['status']}{' ' + t['owner'] if t['owner'] else ''}] {t['title']}"
                  f"{' files=' + ','.join(t['files']) if t['files'] else ''}"
                  f"{' deps=' + ','.join(t['deps']) if t['deps'] else ''}{ready}"
                  f"{' - ' + t['note'] if t['note'] else ''}")
    if a.task_cmd in ("add", "take", "done", "drop", "spec"):
        mark_seen(s, agent, ev)


def change_patch_id(base, head):
    """Content fingerprint of a change: zero-context diff, so it survives a
    clean rebase but changes if the code changes."""
    d = git("diff", "-U0", "--no-color", base, head, check=False).stdout
    if not d.strip():
        return None
    p = subprocess.run(["git", "patch-id", "--stable"], input=d, capture_output=True, text=True)
    return (p.stdout.split() or [None])[0]


def integration_base(head):
    git("fetch", "-q", "--no-tags", REMOTE, check=False)
    tgt = f"{REMOTE}/main"
    mb = git("merge-base", tgt, head, check=False).stdout.strip()
    return mb


def cmd_review(a):
    agent = me()
    if a.review_cmd == "request":
        def build(s):
            cands = s.active_agents(exclude=(agent,))
            if a.self_review:
                if cands:
                    raise CoordError(f"self-review is only allowed when no other agent is live; "
                                     f"live now: {', '.join(cands)}")
                if not a.note:
                    raise CoordError("say why and what you verified: -m '...'")
                reviewer = agent
            elif a.to:
                if a.to not in s.agents or a.to == agent:
                    raise CoordError(f"invalid reviewer {a.to}")
                if not s.is_live(a.to) and not a.force:
                    raise CoordError(f"{a.to} is not live (last seen "
                                     f"{ago(s.agents[a.to]['last_seen'])}); live reviewers: "
                                     f"{', '.join(cands) or 'none'}")
                reviewer = a.to
            else:
                if not cands:
                    raise CoordError(
                        "no other live agent can review. Options: wait for one (`coord wait`), "
                        "escalate (`coord say --needs-human ...`), or if the work cannot wait: "
                        "`coord review request --self -m '<what you verified>'` - an audited "
                        "self-approval listed for humans in `coord digest`.")
                load = {c: sum(1 for r in s.reviews.values()
                               if r["reviewer"] == c and r["status"] == "open") for c in cands}
                reviewer = sorted(cands, key=lambda c: (load[c], c))[0]
            ev = {"type": "review_request", "reviewer": reviewer,
                  "ref": a.ref or current_branch(), "note": a.note or "",
                  "head": head, "patch_id": pid}
            if a.task:
                if a.task not in s.tasks:
                    raise CoordError(f"no task {a.task}")
                ev["task"] = a.task
            if a.self_review:
                ev["self"] = True
                ev["needs_human"] = True
            return ev
        head = None
        for cand in ([a.ref, f"{REMOTE}/{a.ref}"] if a.ref else []) + ["HEAD"]:
            r = git("rev-parse", "--verify", "-q", f"{cand}^{{commit}}", check=False).stdout.strip()
            if r:
                head = r
                break
        base = integration_base(head) if head else None
        pid = change_patch_id(base, head) if base else None
        if base and pid is None and not a.force:
            raise CoordError(f"nothing to review: {a.ref or 'HEAD'} has no changes vs "
                             f"{REMOTE}/main (commit first, or pass --ref <branch>)")
        ev, s = commit_event(build)
        print(f"R{ev['seq']} assigned to {ev['reviewer']} for change {str(ev['patch_id'])[:10]} "
              f"(head {str(ev['head'])[:8]}). Push your branch so they can read it: "
              f"git push origin HEAD:{ev['ref']} ; then `coord wait --review R{ev['seq']}`. "
              f"Approval covers exactly this diff: edit after approval => request again.")
    elif a.review_cmd == "withdraw":
        def build(s):
            r = s.reviews.get(a.rid)
            if not r or r["author"] != agent or r["status"] != "open":
                raise CoordError(f"{a.rid} is not an open review of yours")
            return {"type": "review_verdict_withdraw", "rid": a.rid}
        ev, s = commit_event(build)
        print(f"{a.rid} withdrawn.")
    elif a.review_cmd == "reassign":
        def build(s):
            r = s.reviews.get(a.rid)
            if not r or r["author"] != agent or r["status"] != "open":
                raise CoordError(f"{a.rid} is not an open review of yours")
            waited = now() - r.get("assigned", r["created"])
            if s.is_live(r["reviewer"]) and waited < 600 and not a.to:
                raise CoordError(f"{r['reviewer']} is live and was assigned {fmt_dur(waited)} ago; "
                                 f"reassign after 10m, or name someone with --to")
            if a.to:
                cands = [a.to]
            else:
                cands = s.active_agents(exclude=(agent, r["reviewer"]))
            if not cands:
                raise CoordError("no other live agent available to review")
            load = {c: sum(1 for x in s.reviews.values()
                           if x["reviewer"] == c and x["status"] == "open") for c in cands}
            return {"type": "review_reassign", "rid": a.rid,
                    "reviewer": sorted(cands, key=lambda c: (load[c], c))[0]}
        ev, s = commit_event(build)
        print(f"{a.rid} reassigned to {ev['reviewer']}.")
    elif a.review_cmd in ("approve", "changes"):
        def build(s):
            r = s.reviews.get(a.rid)
            if not r:
                raise CoordError(f"no review {a.rid}")
            if r["reviewer"] != agent:
                raise CoordError(f"{a.rid} is assigned to {r['reviewer']}, not you")
            if r["status"] != "open":
                raise CoordError(f"{a.rid} already {r['status']}")
            if a.review_cmd == "changes" and not a.note:
                raise CoordError("say exactly what must change: -m '...'")
            return {"type": "review_verdict", "rid": a.rid,
                    "verdict": "approved" if a.review_cmd == "approve" else "changes",
                    "note": a.note or ""}
        ev, s = commit_event(build)
        print(f"{a.rid} {ev['verdict']} (#{ev['seq']}).")
    else:
        s = replay(load_events(fetch()))
        for r in s.reviews.values():
            if a.all or r["status"] == "open":
                print(f"{r['id']} [{r['status']}] {r['author']} -> {r['reviewer']}: {r['ref']} "
                      f"- {r['note']}{' | ' + r['verdict_note'] if r['verdict_note'] else ''}")
        return
    mark_seen(s, agent, ev)


def cmd_policy(a):
    if a.key == "show":
        s = replay(load_events(fetch()))
        print(json.dumps(s.policy) if s.policy else "no policies")
        return
    key = a.key.replace("-", "_")
    ev, s = commit_event(lambda st: {"type": "policy", "key": key, "value": a.values})
    print(f"policy {key} = {a.values or '(cleared)'} (#{ev['seq']}); enforced by every "
          f"agent's pre-push guard.")


def cmd_adopt(a):
    agent = me()

    def build(s):
        old = s.agents.get(a.old)
        if not old or a.old == agent:
            raise CoordError(f"no other agent named {a.old}")
        same_human = old.get("human") and old.get("human") == identity().get("human")
        if s.is_live(a.old) and not same_human and not a.force:
            raise CoordError(f"{a.old} is live (seen {ago(old['last_seen'])}); you can only adopt "
                             f"a stale agent's work, or your own human's (--human must match)")
        return {"type": "adopt", "from": a.old, "note": a.note or ""}
    ev, s = commit_event(build)
    print(f"adopted {a.old}'s work (#{ev['seq']}).")
    print_brief(s, agent)


def _hook_payload():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    cwd = payload.get("cwd")
    if cwd and os.path.isdir(cwd):
        os.chdir(cwd)
    return payload


def _emit(event, text):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                             "additionalContext": text[:9500]}}))


def _auto_name():
    base = git("config", "user.name", check=False).stdout.strip() or os.environ.get("USER", "agent")
    base = "".join(ch for ch in base.lower().replace(" ", "-") if ch.isalnum() or ch in "-_.")[:16]
    return f"{base or 'agent'}-{random.randrange(16 ** 4):04x}"


def cmd_session_start(a):
    """SessionStart hook: register (or resume) this session on the bus and brief it.
    Installing this hook is the team's opt-in; Claude needs no instruction."""
    import io
    import contextlib
    try:
        payload = _hook_payload()
        git("rev-parse", "--git-dir")
        ident = identity(required=False)
        session = payload.get("session_id") or ""
        human = ident.get("human") or git("config", "user.name", check=False).stdout.strip()
        args = argparse.Namespace(agent=ident.get("agent") or _auto_name(), human=human,
                                  task=None, branch=None, history=False, no_hooks=False,
                                  role="agent", session=session)
        buf = io.StringIO()
        if ident.get("agent") and session and ident.get("session") == session:
            # same session (resume/compact/clear): just re-brief
            with contextlib.redirect_stdout(buf):
                s = replay(load_events(fetch(quiet_fail=True)))
                print(f"you are agent {ident['agent']} on the coordination bus.")
                print_brief(s, ident["agent"])
        else:
            with contextlib.redirect_stdout(buf):
                cmd_join(args)
        s = replay(load_events(fetch(quiet_fail=True)))
        me_ = identity()["agent"]
        live = [n for n in s.agents if n != me_ and s.is_live(n)
                and s.agents[n].get("role", "agent") == "agent"]
        head = ("This repository coordinates concurrent AI agents through the `communication` "
                "branch (agent-coordination skill, .claude/skills/agent-coordination/SKILL.md; "
                "tool: coord.py). This session is registered as agent "
                f"'{me_}'. ")
        if live:
            head += (f"{len(live)} other live agent(s): {', '.join(live)}. The skill's rules apply: "
                     "claim files before editing shared code, check the inbox between steps, "
                     "follow decisions on the bus. ")
        else:
            head += ("No other agent is live right now; work normally. If one joins, this "
                     "session is notified, and the skill's rules apply from then on. ")
        head += ("State your task once with `coord status '<what you are doing>' --task '<task>'`.")
        _emit("SessionStart", head + "\n" + buf.getvalue())
    except Exception:
        return 0
    return 0


def cmd_session_end(a):
    try:
        payload = _hook_payload()
        ident = identity(required=False)
        if not ident.get("agent"):
            return 0
        s = replay(load_events(fetch(quiet_fail=True)))
        ag = s.agents.get(ident["agent"])
        sid = payload.get("session_id")
        autosave(ident["agent"])
        if ag and (not sid or not ag.get("session") or ag.get("session") == sid):
            os.environ["COORD_AGENT"] = ident["agent"]
            commit_event(lambda st: {"type": "offline",
                                     "note": "session ended; work kept for resume/adoption"})
    except Exception:
        return 0
    return 0


SETTINGS_HOOKS = {
    "SessionStart": [{"hooks": [{"type": "command", "command": "{py} session-start"}]}],
    "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "{py} hook"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command",
                                     "command": "{py} hook --event UserPromptSubmit"}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "{py} session-end"}]}],
}

CLAUDE_MD_SNIPPET = """
<!-- coord:begin -->
## Multi-agent coordination
Several AI agents (and humans' sessions) may work in this repo at the same time.
They coordinate through the `communication` branch using the agent-coordination
skill (`.claude/skills/agent-coordination/SKILL.md`). Sessions are registered
automatically by a SessionStart hook. Before editing code another agent might
touch, claim it; follow decisions and messages from the bus.
<!-- coord:end -->
"""


def cmd_install(a):
    top = git_out("rev-parse", "--show-toplevel")
    script = os.path.abspath(__file__)
    rel = os.path.relpath(script, top)
    py = (f'python3 "$CLAUDE_PROJECT_DIR/{rel}"' if not rel.startswith("..")
          else f'python3 "{script}"')
    path = os.path.join(top, ".claude", "settings.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    settings = read_json(path, {})
    hooks = settings.setdefault("hooks", {})
    for ev, entries in SETTINGS_HOOKS.items():
        lst = hooks.setdefault(ev, [])
        lst[:] = [x for x in lst if "coord.py" not in json.dumps(x)
                  and os.path.basename(script) not in json.dumps(x)]
        for entry in entries:
            lst.append(json.loads(json.dumps(entry).replace("{py}", py.replace('"', '\\"'))))
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    md = os.path.join(top, "CLAUDE.md")
    cur = open(md).read() if os.path.exists(md) else ""
    if "coord:begin" not in cur:
        with open(md, "a") as f:
            f.write(CLAUDE_MD_SNIPPET)
    print(f"updated {os.path.relpath(path, top)} (SessionStart/PostToolUse/UserPromptSubmit/"
          f"SessionEnd hooks) and CLAUDE.md. Commit both so every teammate's sessions join "
          f"automatically. Also add `branches-ignore: [communication, 'wip/**']` to CI push triggers.")


def cmd_inbox(a):
    agent = me()
    s = replay(load_events(fetch()))
    lines = read_inbox(s, agent, mark=not a.peek)
    if not lines:
        print("inbox empty.")
    for ln in lines:
        print(ln)


def cmd_wait(a):
    agent = me()
    deadline = now() + parse_dur(a.timeout)
    interval = parse_dur(a.interval)
    last_renew = now()
    while True:
        s = replay(load_events(fetch(quiet_fail=True)))
        cur = read_json(cursor_path(agent), {})
        lines = compute_inbox(s, agent, cur)
        done = False
        if a.claim:
            c = s.claims.get(a.claim)
            if not c:
                raise CoordError(f"no claim {a.claim}")
            if c["agent"] != agent:   # someone else's claim: wait for it to end
                if c["status"] not in ("active", "queued"):
                    done = True
                    lines.append(f"claim {a.claim} ({c['agent']}) is now {c['status'].upper()}"
                                 f"{': ' + c['note'] if c.get('note') else ''}")
            elif c["status"] != "queued":
                done = True
                lines.append(f"claim {a.claim} is now {c['status'].upper()}")
        elif a.proposal:
            p = s.props.get(a.proposal)
            if not p:
                raise CoordError(f"no proposal {a.proposal}")
            if p["status"] != "open":
                done = True
        elif a.review:
            r = s.reviews.get(a.review)
            if not r:
                raise CoordError(f"no review {a.review}")
            if r["status"] != "open":
                done = True
                lines.append(f"review {a.review} is {r['status'].upper()}: {r['verdict_note']}")
        elif a.task:
            t = s.tasks.get(a.task)
            if not t:
                raise CoordError(f"no task {a.task}")
            if t["status"] == "done":
                done = True
                lines.append(f"task {a.task} is DONE: {t['note']}")
        else:
            done = bool(lines)
        actionable = [ln for ln in lines if any(k in ln for k in (
            "REVIEW REQUEST", "URGENT", "HANDOFF", "INCIDENT", "REVIEWER STALE", "[requirement]",
            "WAITING ON YOU"))]
        if not done and actionable:
            done = True
            lines.append("(returned early: the items above need action from you; resume your "
                         "wait afterwards)")
        if done:
            write_json(cursor_path(agent), snapshot_cursor(s, agent))
            for ln in lines:
                print(ln)
            return 0
        if now() >= deadline:
            if lines:
                write_json(cursor_path(agent), snapshot_cursor(s, agent))
                for ln in lines:
                    print(ln)
            status = ""
            if a.claim and a.claim in s.claims:
                status = " now: " + fmt_claim(s.claims[a.claim], s)
            elif a.proposal and a.proposal in s.props:
                p = s.props[a.proposal]
                status = (f" now: {p['id']} {len(p['votes'])}/{len(p['electorate']) - 1} voted, "
                          f"closes {ago(p['deadline'])}")
            elif a.review and a.review in s.reviews:
                r = s.reviews[a.review]
                status = f" now: {r['id']} still with {r['reviewer']}"
            print(f"TIMEOUT after {a.timeout}, nothing new;{status}. Call wait again "
                  f"(exit code 2 just means timeout).")
            return 2
        # keep our leases alive while we block, and stay visibly live: a patient
        # waiter must not look like a dead session to the rest of the team
        mine = [c for c in s.live_claims() if c["agent"] == agent]
        seen = s.agents.get(agent, {}).get("last_seen", now())
        if ((mine and min(c["expires"] for c in mine) - now() < DEFAULT_TTL / 3)
                or now() - seen > LIVE_WINDOW / 3) and now() - last_renew > 60:
            commit_event(lambda st: {"type": "renew"})
            last_renew = now()
        time.sleep(interval * random.uniform(0.8, 1.2))


def cmd_board(a):
    s = replay(load_events(fetch()))
    if a.markdown:
        print(render_board(s))
        return
    print(f"seq {s.seq} | {len(s.agents)} agents | branch {REMOTE}/{BRANCH}")
    for ag in s.agents.values():
        st = ag["status"]
        if st == "active" and not s.is_live(ag["name"]):
            st = "offline" if ag.get("offline") else "stale"
        print(f"  agent {ag['name']} [{st}] {ag['task']} (human: {ag['human'] or '-'}, "
              f"seen {ago(ag['last_seen'])})")
    for c in s.live_claims():
        print("  claim " + fmt_claim(c, s))
    for t in s.tasks.values():
        if t["status"] != "done":
            print(f"  task {t['id']} [{t['status']}{' ' + t['owner'] if t['owner'] else ''}] {t['title']}")
    for p in s.props.values():
        if p["status"] == "open":
            print(f"  proposal {p['id']} OPEN {p['title']} default={p['default']} "
                  f"votes={len(p['votes'])}/{len(p['electorate']) - 1} closes {ago(p['deadline'])}")
    g = s.waits_for()
    if g:
        print("  waits-for: " + "; ".join(f"{k}->{','.join(sorted(v))}" for k, v in g.items()))


def cmd_log(a):
    s = replay(load_events(fetch()))
    for e in s.events[-a.n:]:
        print(f"{iso(e['t'])} " + fmt_event(e))


def cmd_decisions(a):
    s = replay(load_events(fetch()))
    for p in s.props.values():
        votes = "; ".join(f"{k}:{v['stance']}{'->' + v['alt'] if v.get('alt') else ''}"
                          for k, v in p["votes"].items())
        about = f" about={','.join(p['about'])}" if p.get("about") else ""
        print(f"{p['id']} [{p['status']}] {p['title']}{about} -> "
              f"{p.get('outcome', '(open) default: ' + p['default'])}"
              f"{' | votes ' + votes if votes else ''}{alts_text(p)}")


def cmd_digest(a):
    """A summary meant for the humans: what happened, what was decided,
    what (if anything) needs them."""
    s = replay(load_events(fetch()))
    since = now() - parse_dur(a.since) if a.since else 0
    ev = [e for e in s.events if e["t"] >= since]
    print(f"# Coordination digest ({len(ev)} events, {len(s.agents)} agents)\n")
    answered = {e.get("re") for e in s.events if e["type"] == "msg" and e.get("re")
                and s.agents.get(e["agent"], {}).get("role") == "human"}
    esc = [e for e in ev if e.get("needs_human") and e["seq"] not in answered] + \
          [p for p in s.props.values() if p["needs_human"] and p["status"] == "open"
           and p["created"] >= since]
    print("## Needs a human")
    if not esc:
        print("- nothing")
    for e in esc:
        if "type" in e:
            label = "SELF-APPROVED review (audit it)" if e.get("self") else e["type"]
            print(f"- #{e['seq']} {e['agent']} [{label}]: "
                  f"{e.get('body') or e.get('title') or e.get('note', '')}"
                  f"{' (ref ' + e['ref'] + ')' if e.get('ref') else ''}")
        else:
            print(f"- {e['id']} {e['title']} [{e['status']}] -> {e.get('outcome', 'open')}")
    done_esc = [e for e in ev if e.get("needs_human") and e["seq"] in answered]
    if done_esc:
        print("\n## Escalations answered by humans")
        for e in done_esc:
            ans = [m for m in s.events if m.get("re") == e["seq"]]
            print(f"- #{e['seq']} {e['agent']}: {(e.get('body') or e.get('note', ''))[:120]} -> "
                  f"{ans[-1]['agent']}: {ans[-1].get('body', '')[:160]}")
    print("\n## Decisions made by agents")
    for p in s.props.values():
        if p["created"] >= since:
            out = p.get("outcome") or ("withdrawn" if p["status"] == "withdrawn" else "open")
            out = out if len(out) <= 200 else out[:197] + "..."
            print(f"- {p['id']} {p['title']}: **{out}** ({p['status']})")
    print("\n## Work")
    for t in s.tasks.values():
        print(f"- {t['id']} {t['title']}: {t['status']}{' by ' + t['owner'] if t['owner'] else ''}"
              f"{' - ' + t['note'] if t['note'] else ''}")
    rel = [c for c in s.claims.values() if c.get("note") and c.get("ended", 0) >= since]
    if rel:
        print("\n## Integrations (release notes)")
        for c in rel:
            print(f"- {c['agent']} {' '.join(c['resources'])}: {c['note']}")
    kinds = {}
    for e in ev:
        kinds[e["type"]] = kinds.get(e["type"], 0) + 1
    print("\n## Traffic\n- " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))


def changed_files(base, staged_only):
    files = set()
    if staged_only:
        files |= set(git_out("diff", "--cached", "--name-only").splitlines())
    else:
        mb = git("merge-base", "HEAD", base, check=False).stdout.strip()
        if mb:
            files |= set(git_out("diff", "--name-only", mb).splitlines())
        files |= set(git_out("diff", "--name-only").splitlines())
        files |= set(git_out("diff", "--cached", "--name-only").splitlines())
        files |= set(git_out("ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(f for f in files if f)


def cmd_guard(a):
    agent = me()
    s = replay(load_events(fetch(quiet_fail=True)))
    if a.push:
        # pre-push hook: stdin has "<local ref> <local sha> <remote ref> <remote sha>"
        bad = []
        for line in sys.stdin.read().splitlines():
            parts = line.split()
            if len(parts) < 3 or not parts[2].startswith("refs/heads/"):
                continue
            branch = parts[2][len("refs/heads/"):]
            if branch == BRANCH:
                continue
            if branch in (s.policy.get("require_review") or []) and \
                    parts[1] != "0" * 40 and len(parts) >= 4 and parts[3] != "0" * 40:
                pid = change_patch_id(parts[3], parts[1])
                ok = pid is None or any(
                    r["status"] == "approved" and r["author"] == agent and r.get("patch_id") == pid
                    for r in s.reviews.values())
                if not ok:
                    bad.append(f"{branch} requires an approved review of exactly this change "
                               f"(patch {str(pid)[:10]}). Run `coord review request -m ...` and "
                               f"wait for approval; if you changed code or resolved conflicts "
                               f"after approval, request again")
            for c in s.live_claims(("active",)):
                if c["agent"] != agent and c["mode"] == "exclusive" and \
                        f"@freeze:{branch}" in c["resources"]:
                    bad.append(f"{branch} is FROZEN by {c['agent']} ({c['id']}: {c['intent']})")
        for b in bad:
            print("coord pre-push: " + b, file=sys.stderr)
        if bad:
            print("coord pre-push: push refused by team policy (see above).", file=sys.stderr)
            return 1
        return 0
    files = changed_files(a.base, a.staged)
    problems, warnings = [], []
    live = s.live_claims(("active",))
    for f in files:
        mine = [c for c in live if c["agent"] == agent and
                any(paths_overlap(split_res(r)[0], f) for r in c["resources"])]
        theirs = [c for c in live if c["agent"] != agent and
                  any(paths_overlap(split_res(r)[0], f) for r in c["resources"])]
        for c in theirs:
            whole = any(split_res(r)[1] is None and paths_overlap(split_res(r)[0], f)
                        for r in c["resources"])
            if whole and c["mode"] == "exclusive":
                problems.append(f"{f}: whole-file claim {c['id']} held by {c['agent']} ({c['intent']})")
            elif not mine:
                problems.append(f"{f}: {c['agent']} holds region claim {c['id']} "
                                f"({', '.join(c['resources'])}) and you hold none on this file")
            else:
                warnings.append(f"{f}: shared with {c['agent']} ({', '.join(c['resources'])}) - "
                                f"stay inside your region")
        if a.strict and not mine:
            problems.append(f"{f}: changed without any claim of yours")
    for w in warnings:
        print("warn: " + w)
    for p in problems:
        print("CONFLICT: " + p)
    if problems:
        print("guard: FAIL - claim the resource (and wait) or move the change.")
        return 1
    print(f"guard: ok ({len(files)} changed file(s) checked)")
    return 0


def cmd_hook(a):
    """Claude Code hook: inject unread coordination messages into context.
    Never fails the tool call; rate-limited by COORD_HOOK_INTERVAL seconds."""
    try:
        try:
            payload = json.load(sys.stdin)
        except Exception:
            payload = {}
        cwd = payload.get("cwd")
        if cwd and os.path.isdir(cwd):
            os.chdir(cwd)
        ident = identity(required=False)
        if not ident.get("agent"):
            return 0
        agent = ident["agent"]
        stamp = os.path.join(state_dir(), "hook-stamp")
        interval = float(os.environ.get("COORD_HOOK_INTERVAL", "30"))
        try:
            if now() - os.path.getmtime(stamp) < interval:
                return 0
        except OSError:
            pass
        with open(stamp, "w") as f:
            f.write(str(now()))
        s = replay(load_events(fetch(quiet_fail=True)))
        ag = s.agents.get(agent, {})
        sid = payload.get("session_id")
        if sid and ag.get("session") and ag["session"] != sid and ag.get("status") == "active":
            event = payload.get("hook_event_name") or a.event
            _emit(event, f"coord bus: identity '{agent}' was resumed by ANOTHER session "
                         f"({ag['session'][:8]}) - this session is superseded. Its claims and "
                         f"tasks now belong to that session. Stop editing shared files; to take "
                         f"the identity back run `coord join`.")
            return 0
        autosave(agent)
        # heartbeat: live sessions that others depend on stay visibly live and keep
        # their leases; crashed sessions go stale quickly and free their locks/tasks
        owned = s.owned_by(agent) if agent in s.agents else {}
        mine = owned.get("claims", [])
        need = (mine and min(c["expires"] - now() for c in mine) < min(c["ttl"] for c in mine) / 2) \
            or (any(owned.values()) and now() - ag.get("last_seen", 0) > LIVE_WINDOW / 3)
        if need:
            os.environ["COORD_AGENT"] = agent
            _, s = commit_event(lambda st: {"type": "renew"})
        lines = read_inbox(s, agent, mark=True)
        if not lines:
            return 0
        event = payload.get("hook_event_name") or a.event
        text = (f"coord bus: {len(lines)} unread coordination event(s) for agent {agent} "
                f"(already marked read):\n" + "\n".join(lines[:20]))
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                                 "additionalContext": text}}))
    except Exception:
        return 0
    return 0


def install_guard_hook():
    hooks = git_out("rev-parse", "--git-path", "hooks")
    os.makedirs(hooks, exist_ok=True)
    path = os.path.join(hooks, "pre-commit")
    script = os.path.abspath(__file__)
    body = (f"#!/bin/sh\n# installed by coord: blocks commits touching files other agents "
            f"have claimed\nCOORD_AGENT=\"${{COORD_AGENT:-}}\" exec python3 '{script}' guard --staged\n")
    if os.path.exists(path):
        cur = open(path).read()
        if "installed by coord" not in cur:
            raise CoordError(f"{path} exists; add `python3 {script} guard --staged` to it yourself")
        if cur == body:
            return ""
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o755)
    pp = os.path.join(hooks, "pre-push")
    if not os.path.exists(pp) or "installed by coord" in open(pp).read():
        with open(pp, "w") as f:
            f.write(f"#!/bin/sh\n# installed by coord: blocks pushes to frozen branches\n"
                    f"exec python3 '{script}' guard --push\n")
        os.chmod(pp, 0o755)
    return (f"git guards installed: pre-commit blocks commits touching files claimed by others; "
            f"pre-push blocks pushes to a branch someone froze with `claim @freeze:<branch>`.")


def cmd_install_hooks(a):
    """Install a git pre-commit hook that runs `coord guard --staged`."""
    print(install_guard_hook() or "pre-commit guard already installed")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="coord", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"coord {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("join", help="register this agent on the bus")
    p.add_argument("--agent")
    p.add_argument("--human", help="the human this agent works for")
    p.add_argument("--task", help="one line: what you are doing")
    p.add_argument("--branch", help="your work branch (default: current)")
    p.add_argument("--history", action="store_true", help="mark entire history unread")
    p.add_argument("--no-hooks", action="store_true", help="don't install the git guards")
    p.add_argument("--session", help="Claude session id (set automatically by the hook)")
    p.add_argument("--role", choices=["agent", "human"], default="agent",
                   help="human = observer/stakeholder: can post, never votes or reviews")
    p.set_defaults(fn=cmd_join)

    p = sub.add_parser("leave", help="leave the bus, releasing all claims")
    p.add_argument("--note")
    p.add_argument("--unmerged", metavar="REASON",
                   help="leave with work not on main (becomes a STRANDED task)")
    p.set_defaults(fn=cmd_leave)

    p = sub.add_parser("status", help="update your status line")
    p.add_argument("note")
    p.add_argument("--task")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("say", help="send a message")
    p.add_argument("text", nargs="+")
    p.add_argument("--to", default="all", help="agent name(s), comma separated, or 'all'")
    p.add_argument("--kind", choices=["info", "question", "answer", "decision", "handoff",
                                      "contract", "blocker", "incident", "requirement"])
    p.add_argument("--re", type=int, help="seq of the message you reply to")
    p.add_argument("--needs-human", action="store_true")
    p.set_defaults(fn=cmd_say)

    p = sub.add_parser("claim", help="lease files/regions before editing them")
    p.add_argument("resources", nargs="+",
                   help="path, dir/, glob, path#region or @pseudo-resource")
    p.add_argument("--intent", "-m", help="what you will change")
    p.add_argument("--ttl", help="lease length, e.g. 20m (default 30m)")
    p.add_argument("--shared", action="store_true", help="shared (read/append) lease")
    p.add_argument("--no-queue", action="store_true", help="fail instead of queueing")
    p.add_argument("--urgent", action="store_true",
                   help="hotfix/incident only: jump ahead of normal queued claims and "
                        "ask holders to release at their next safe point")
    p.set_defaults(fn=cmd_claim)

    p = sub.add_parser("release", help="release claims (default: all of yours)")
    p.add_argument("what", nargs="*", help="claim ids or resources")
    p.add_argument("--all", action="store_true")
    p.add_argument("--note", "-m", help="e.g. 'merged to main as abc123'")
    p.add_argument("--unmerged", metavar="REASON",
                   help="release work that is NOT on main (becomes a STRANDED task)")
    p.set_defaults(fn=cmd_release)

    p = sub.add_parser("renew", help="extend your leases")
    p.set_defaults(fn=cmd_renew)

    p = sub.add_parser("propose", help="propose a decision (lazy consensus)")
    p.add_argument("title")
    p.add_argument("--default", required=True, help="what happens if nobody objects")
    p.add_argument("--body", help="context / rationale")
    p.add_argument("--deadline", help="e.g. 5m (default 10m)")
    p.add_argument("--about", nargs="+", metavar="RES",
                   help="resource(s) this decision governs, e.g. store/cart.py#total; "
                        "only one open proposal per resource")
    p.add_argument("--topic", help="short slug; only one open proposal per topic")
    p.add_argument("--reopen", action="store_true", help="reopen a decided question")
    p.add_argument("--force", action="store_true", help="ignore similar-title check")
    p.add_argument("--needs-human", action="store_true")
    p.set_defaults(fn=cmd_propose)

    p = sub.add_parser("vote", help="accept or object to a proposal")
    p.add_argument("pid")
    p.add_argument("stance", choices=["accept", "object"])
    p.add_argument("--alt", help="required for object: your concrete alternative")
    p.add_argument("--reason")
    p.set_defaults(fn=cmd_vote)

    p = sub.add_parser("withdraw", help="withdraw your open proposal")
    p.add_argument("pid")
    p.set_defaults(fn=cmd_withdraw)

    p = sub.add_parser("task", help="shared task board")
    tsub = p.add_subparsers(dest="task_cmd", required=True)
    q = tsub.add_parser("add")
    q.add_argument("items", nargs="+", help="'ID: title files=a,b deps=T1'")
    q.add_argument("--spec", help="verbatim requirement / acceptance criteria (travels with "
                                  "the task through handoffs and into reviews)")
    q = tsub.add_parser("spec", help="attach verbatim acceptance criteria to a task")
    q.add_argument("tid")
    q.add_argument("text")
    q = tsub.add_parser("take")
    q.add_argument("tid")
    q.add_argument("--force", action="store_true", help="take even if deps are not done")
    q = tsub.add_parser("done")
    q.add_argument("tid")
    q.add_argument("--note", "-m")
    q = tsub.add_parser("drop", help="give a task back (handoff with --to)")
    q.add_argument("tid")
    q.add_argument("--note", "-m", help="state of the work: branch, what is done, what is left")
    q.add_argument("--to", help="agent to hand it to, or 'any'")
    tsub.add_parser("list")
    p.set_defaults(fn=cmd_task)
    p = sub.add_parser("tasks", help="list tasks")
    p.set_defaults(fn=cmd_task, task_cmd="list")

    p = sub.add_parser("review", help="code review between agents")
    rsub = p.add_subparsers(dest="review_cmd", required=True)
    q = rsub.add_parser("request", help="ask for a review (auto-assigns least-loaded agent)")
    q.add_argument("--to", help="specific reviewer")
    q.add_argument("--ref", help="branch to review (default: current branch)")
    q.add_argument("--note", "-m", help="what changed / what to look at")
    q.add_argument("--force", action="store_true", help="request even with no diff vs main")
    q.add_argument("--task", help="task id: its acceptance criteria are shown to the reviewer")
    q.add_argument("--self", dest="self_review", action="store_true",
                   help="audited self-approval; only when no other agent is live")
    q = rsub.add_parser("approve")
    q.add_argument("rid")
    q.add_argument("--note", "-m")
    q = rsub.add_parser("changes", help="request changes (must say what)")
    q.add_argument("rid")
    q.add_argument("--note", "-m")
    q = rsub.add_parser("withdraw", help="withdraw your open review request")
    q.add_argument("rid")
    q = rsub.add_parser("reassign", help="move your review to a live reviewer")
    q.add_argument("rid")
    q.add_argument("--to")
    q = rsub.add_parser("list")
    q.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("adopt", help="take over a stale agent's claims, tasks and reviews")
    p.add_argument("old")
    p.add_argument("--note", "-m")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_adopt)

    p = sub.add_parser("session-start", help="Claude Code SessionStart hook (auto join/resume)")
    p.set_defaults(fn=cmd_session_start)
    p = sub.add_parser("session-end", help="Claude Code SessionEnd hook (mark offline)")
    p.set_defaults(fn=cmd_session_end)
    p = sub.add_parser("install", help="add coord hooks to .claude/settings.json + CLAUDE.md")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("policy", help="team policies enforced by git hooks")
    p.add_argument("key", help="require-review | show")
    p.add_argument("values", nargs="*", help="branches (empty = clear)")
    p.set_defaults(fn=cmd_policy)

    p = sub.add_parser("inbox", help="show unread notifications")
    p.add_argument("--peek", action="store_true", help="don't mark as read")
    p.set_defaults(fn=cmd_inbox)

    p = sub.add_parser("wait", help="block until something relevant happens")
    p.add_argument("--claim", help="until this claim is granted")
    p.add_argument("--proposal", help="until this proposal resolves")
    p.add_argument("--task", help="until this task is done")
    p.add_argument("--review", help="until this review has a verdict")
    p.add_argument("--timeout", default="100s",
                   help="default 100s: stays under a 2-minute tool-call limit; loop if needed")
    p.add_argument("--interval", default="10s")
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("board", help="show current state")
    p.add_argument("--markdown", action="store_true")
    p.set_defaults(fn=cmd_board)

    p = sub.add_parser("log", help="recent raw events")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("decisions", help="all proposals and outcomes")
    p.set_defaults(fn=cmd_decisions)

    p = sub.add_parser("digest", help="summary for humans")
    p.add_argument("--since", help="e.g. 24h")
    p.set_defaults(fn=cmd_digest)

    p = sub.add_parser("guard", help="check your changes against others' claims")
    p.add_argument("--base", default=f"{REMOTE}/main")
    p.add_argument("--staged", action="store_true")
    p.add_argument("--push", action="store_true", help="pre-push mode (reads refs on stdin)")
    p.add_argument("--strict", action="store_true", help="every changed file must be claimed")
    p.set_defaults(fn=cmd_guard)

    p = sub.add_parser("hook", help="Claude Code hook entrypoint (prints additionalContext)")
    p.add_argument("--event", default="PostToolUse")
    p.set_defaults(fn=cmd_hook)

    p = sub.add_parser("install-hooks", help="install git pre-commit guard")
    p.set_defaults(fn=cmd_install_hooks)

    a = ap.parse_args(argv)
    try:
        rc = a.fn(a)
        return rc or 0
    except CoordError as e:
        print(f"coord: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
