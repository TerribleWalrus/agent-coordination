"""End-to-end tests for coord.py against a throwaway bare remote.

Run from anywhere:  python3 tests/test_coord.py   (inside the skill folder)
Test another copy:  COORD=/path/to/coord.py python3 tests/test_coord.py
Needs git and Python 3.8+; takes about a minute.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
COORD = os.environ.get("COORD") or os.path.join(HERE, "..", "scripts", "coord.py")
os.environ.pop("COORD_AGENT", None)   # an exported identity would leak into every test agent


def sh(*args, cwd=None, check=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(f"{args} failed: {p.stdout}{p.stderr}")
    return p


class Bus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coordtest-")
        sh("git", "init", "-q", "--bare", "origin.git", cwd=self.tmp)

    def agent(self, name, join=True):
        d = os.path.join(self.tmp, name)
        if not os.path.exists(d):
            sh("git", "clone", "-q", "origin.git", name, cwd=self.tmp)
        if join:
            self.c(name, "join", "--agent", name, "--task", f"{name} task")
        return d

    def c(self, name, *args, ok=True):
        p = subprocess.run([sys.executable, COORD, *args], cwd=os.path.join(self.tmp, name),
                           capture_output=True, text=True)
        if ok is True and p.returncode != 0:
            raise AssertionError(f"coord {args} as {name} failed:\n{p.stdout}{p.stderr}")
        if ok is False and p.returncode == 0:
            raise AssertionError(f"coord {args} as {name} unexpectedly succeeded:\n{p.stdout}")
        return p.stdout + p.stderr

    # ------------------------------------------------------------------
    def test_concurrent_claims_single_grant_and_no_gaps(self):
        names = [f"a{i}" for i in range(8)]
        for n in names:
            self.agent(n)
        with ThreadPoolExecutor(len(names)) as ex:
            outs = list(ex.map(lambda n: self.c(n, "claim", "hot.py", "-m", "race"), names))
        self.assertEqual(sum(o.startswith("GRANTED") for o in outs), 1, outs)
        self.assertEqual(sum(o.startswith("QUEUED") for o in outs), len(names) - 1)
        log = self.c("a0", "log", "-n", "1000")
        seqs = [int(l.split()[1][1:]) for l in log.splitlines()]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

    def test_regions_parallel_and_whole_file_serialises(self):
        self.agent("a"); self.agent("b"); self.agent("c")
        self.assertIn("GRANTED", self.c("a", "claim", "cart.py#total"))
        self.assertIn("GRANTED", self.c("b", "claim", "cart.py#add_item"))
        self.assertIn("QUEUED", self.c("c", "claim", "cart.py"))
        self.c("a", "release", "-m", "merged")
        self.assertIn("queued c cart.py", self.c("c", "board"))
        out = self.c("b", "release", "-m", "merged")
        self.assertIn("next holder: c", out)
        self.assertIn("GRANTED", self.c("c", "inbox"))

    def test_fifo_bypass_avoids_false_deadlock(self):
        # round-1 bug: dave held config.py; alice queued for config.py+tests;
        # dave then claimed tests and was refused as DEADLOCK.
        self.agent("dave"); self.agent("alice")
        self.assertIn("GRANTED", self.c("dave", "claim", "config.py"))
        self.assertIn("QUEUED", self.c("alice", "claim", "config.py", "tests.py"))
        out = self.c("dave", "claim", "tests.py")
        self.assertIn("GRANTED", out)

    def test_real_deadlock_refused(self):
        self.agent("a"); self.agent("b")
        self.c("a", "claim", "x.py"); self.c("b", "claim", "y.py")
        self.assertIn("QUEUED", self.c("a", "claim", "y.py"))
        out = self.c("b", "claim", "x.py", ok=False)
        self.assertIn("DEADLOCK", out)

    def test_lease_expiry_hands_over(self):
        self.agent("a"); self.agent("b")
        self.c("a", "claim", "f.py", "--ttl", "2s")
        self.assertIn("QUEUED", self.c("b", "claim", "f.py"))
        out = self.c("b", "wait", "--claim", "C4", "--interval", "1s", "--timeout", "20s")
        self.assertIn("ACTIVE", out)

    def test_duplicate_proposal_refused_and_alternatives_pool(self):
        for n in ("a", "b", "c", "d"):
            self.agent(n)
        self.c("a", "propose", "order of ops in total", "--about", "cart.py#total",
               "--default", "discount,shipping,tax", "--deadline", "1h")
        out = self.c("b", "propose", "total pipeline", "--about", "cart.py#total",
                     "--default", "tax,discount", ok=False)
        self.assertIn("DUPLICATE", out)
        # similar title without --about is also caught
        out = self.c("b", "propose", "Order of ops in total()", "--topic", "totals", "--default", "x", ok=False)
        self.assertIn("DUPLICATE", out)
        self.c("b", "vote", "P5", "object", "--alt", "tax on shipping too")
        self.c("c", "vote", "P5", "object", "--alt", "A1")
        out = self.c("d", "vote", "P5", "object", "--alt", "A1")
        self.assertIn("resolved: decided 'tax on shipping too'", out)
        out = self.c("a", "propose", "redo", "--about", "cart.py#total", "--default", "y",
                     ok=False)
        self.assertIn("ALREADY DECIDED", out)

    def test_silence_is_consent_and_tie_goes_to_default(self):
        self.agent("a"); self.agent("b"); self.agent("c")
        self.c("a", "propose", "naming", "--topic", "naming", "--default", "snake",
               "--deadline", "2s")
        self.c("b", "vote", "P4", "object", "--alt", "camel")
        time.sleep(3)
        self.assertIn("-> snake", self.c("c", "decisions"))
        self.assertIn("DECIDED P4", self.c("c", "inbox"))

    def test_task_board_cas(self):
        self.agent("a"); self.agent("b")
        self.c("a", "task", "add", "T1: one files=a.py", "T2: two deps=T1")
        self.c("b", "task", "add", "T1: dup", ok=False)
        self.c("a", "task", "take", "T1")
        self.assertIn("TAKEN", self.c("b", "task", "take", "T1", ok=False))
        self.assertIn("BLOCKED", self.c("b", "task", "take", "T2", ok=False))
        self.assertIn("unblocked: T2", self.c("a", "task", "done", "T1"))
        self.c("b", "task", "take", "T2")

    def test_guard_flags_foreign_whole_file_claim(self):
        d = self.agent("a"); self.agent("b")
        sh("git", "-C", d, "commit", "-q", "--allow-empty", "-m", "init",
           "-c", "user.name=a", check=False)
        self.c("b", "claim", "shared.py")
        with open(os.path.join(d, "shared.py"), "w") as f:
            f.write("x = 1\n")
        out = self.c("a", "guard", "--base", "HEAD", ok=False)
        self.assertIn("CONFLICT", out)

    def test_precommit_guard_blocks_unclaimed_edit(self):
        d = self.agent("a"); self.agent("b")
        self.c("b", "claim", "shared.py#total", "-m", "b edits total")
        with open(os.path.join(d, "shared.py"), "w") as f:
            f.write("x = 1\n")
        sh("git", "add", "shared.py", cwd=d)
        p = sh("git", "-c", "user.name=a", "-c", "user.email=a@x", "commit", "-qm", "x",
               cwd=d, check=False)
        self.assertNotEqual(p.returncode, 0, "commit should be blocked")
        self.assertIn("CONFLICT", p.stdout + p.stderr)
        self.c("a", "claim", "shared.py#other", "-m", "a edits other")
        p = sh("git", "-c", "user.name=a", "-c", "user.email=a@x", "commit", "-qm", "x",
               cwd=d, check=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_urgent_claim_jumps_queue_and_notifies_holder(self):
        self.agent("a"); self.agent("b"); self.agent("hot")
        self.c("a", "claim", "cart.py", "-m", "feature")
        self.c("b", "claim", "cart.py", "-m", "feature 2")
        out = self.c("hot", "claim", "cart.py", "--urgent", "-m", "prod bug")
        self.assertIn("QUEUED", out)
        self.assertIn("URGENT WAITING ON YOU", self.c("a", "inbox"))
        out = self.c("a", "release", "-m", "stashed for hotfix")
        self.assertIn("next holder: hot", out)

    def test_review_flow_auto_assigns_and_excludes_humans(self):
        self.agent("a"); self.agent("b")
        self.agent("pm", join=False)
        self.c("pm", "join", "--agent", "pm", "--role", "human", "--task", "product")
        out = self.c("a", "review", "request", "--ref", "feat/a", "-m", "coupon change")
        self.assertIn("assigned to b", out)
        rid = out.split()[0]
        self.assertIn("REVIEW REQUEST", self.c("b", "inbox"))
        self.c("b", "review", "changes", rid, ok=False)  # must say what
        self.c("b", "review", "changes", rid, "-m", "add a test for HALF")
        self.assertIn("CHANGES by b", self.c("a", "inbox"))
        out = self.c("a", "propose", "q", "--topic", "q", "--default", "x")
        self.assertIn("Electorate: a, b.", out)  # human pm not in electorate

    def test_handoff_to_agent(self):
        self.agent("a"); self.agent("b")
        self.c("a", "task", "add", "T1: api routes")
        self.c("a", "task", "take", "T1")
        self.c("a", "task", "drop", "T1", "--to", "b", "-m", "branch feat/a has 2 of 3 routes")
        self.assertIn("HANDOFF T1", self.c("b", "inbox"))
        self.c("b", "task", "take", "T1")

    def test_freeze_blocks_push_to_main(self):
        d = self.agent("a"); self.agent("rel")
        sh("git", "-c", "user.name=a", "-c", "user.email=a@x", "commit", "-q",
           "--allow-empty", "-m", "x", cwd=d)
        self.c("rel", "claim", "@freeze:main", "-m", "release v1.0")
        p = sh("git", "push", "-q", "origin", "HEAD:main", cwd=d, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("FROZEN", p.stderr)
        self.c("rel", "release", "-m", "v1.0 tagged")
        p = sh("git", "push", "-q", "origin", "HEAD:main", cwd=d, check=False)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_review_policy_enforced_on_push_and_bound_to_content(self):
        d = self.agent("a"); self.agent("b")
        g = ["git", "-c", "user.name=a", "-c", "user.email=a@x"]
        def commit(text):
            with open(os.path.join(d, "f.py"), "a") as f:
                f.write(text)
            sh("git", "add", "f.py", cwd=d)
            sh(*g, "commit", "-qm", text.strip(), cwd=d)
        commit("v = 1\n")
        sh("git", "push", "-q", "origin", "HEAD:main", cwd=d)          # no policy yet
        self.c("b", "policy", "require-review", "main")
        commit("w = 2\n")
        p = sh("git", "push", "-q", "origin", "HEAD:main", cwd=d, check=False)
        self.assertNotEqual(p.returncode, 0, "unreviewed push must be refused")
        self.assertIn("requires an approved review", p.stderr)
        out = self.c("a", "review", "request", "-m", "add w")
        rid = out.split()[0]
        self.c("b", "review", "approve", rid, "-m", "lgtm")
        p = sh("git", "push", "-q", "origin", "HEAD:main", cwd=d, check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        commit("x = 3\n")                                            # new, unreviewed change
        p = sh("git", "push", "-q", "origin", "HEAD:main", cwd=d, check=False)
        self.assertNotEqual(p.returncode, 0)

    def test_creator_can_hand_off_untaken_task(self):
        self.agent("a"); self.agent("b")
        self.c("a", "task", "add", "T9: leftover")
        self.c("a", "task", "drop", "T9", "--to", "any", "-m", "not started")
        self.assertIn("HANDOFF T9", self.c("b", "inbox"))

    # ---- real-world session lifecycle ---------------------------------
    def hook(self, name, cmd, payload, env=None):
        e = dict(os.environ, **(env or {}))
        e.pop("COORD_AGENT", None)
        p = subprocess.run([sys.executable, COORD, *cmd], cwd=os.path.join(self.tmp, name),
                           input=json.dumps(payload), capture_output=True, text=True, env=e)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"] if p.stdout else ""

    def test_session_start_auto_joins_and_sees_earlier_solo_session(self):
        d1 = self.agent("m1", join=False); d2 = self.agent("m2", join=False)
        ctx1 = self.hook("m1", ["session-start"], {"session_id": "s1", "cwd": d1})
        self.assertIn("No other agent is live", ctx1)
        ctx2 = self.hook("m2", ["session-start"], {"session_id": "s2", "cwd": d2})
        self.assertIn("1 other live agent", ctx2)
        # the first (solo) session learns about the newcomer through its tool hook
        ctx = self.hook("m1", ["hook"], {"session_id": "s1", "cwd": d1,
                                         "hook_event_name": "PostToolUse"})
        self.assertIn("join", ctx)

    def test_new_instance_resumes_identity_and_old_one_is_superseded(self):
        d = self.agent("w", join=False)
        self.hook("w", ["session-start"], {"session_id": "old", "cwd": d})
        self.c("w", "claim", "x.py", "-m", "half done")
        ctx = self.hook("w", ["session-start"], {"session_id": "new", "cwd": d})
        self.assertIn("resumed identity", ctx)
        self.assertIn("YOU OWN", ctx)
        self.assertIn("x.py", ctx)
        ctx = self.hook("w", ["hook"], {"session_id": "old", "cwd": d,
                                        "hook_event_name": "PostToolUse"},
                        env={"COORD_HOOK_INTERVAL": "0"})
        self.assertIn("superseded", ctx)

    def test_stale_owner_task_takeover_and_vote_exclusion(self):
        env_live = {"COORD_LIVE": "2"}
        self.agent("a"); self.agent("b"); self.agent("c")
        self.c("a", "task", "add", "T1: thing")
        self.c("a", "task", "take", "T1")
        self.assertIn("TAKEN", self.c("b", "task", "take", "T1", ok=False))
        time.sleep(3)
        p = subprocess.run([sys.executable, COORD, "task", "take", "T1"],
                           cwd=os.path.join(self.tmp, "b"), capture_output=True, text=True,
                           env=dict(os.environ, **env_live))
        self.assertEqual(p.returncode, 0, p.stderr)
        p = subprocess.run([sys.executable, COORD, "propose", "q", "--topic", "q", "--default",
                            "x"], cwd=os.path.join(self.tmp, "c"), capture_output=True, text=True,
                           env=dict(os.environ, **env_live))
        self.assertIn("Electorate: b, c.", p.stdout)  # a is stale (b just acted)

    def test_adopt_dead_session_by_same_human(self):
        self.agent("old", join=False); self.agent("new", join=False); self.agent("z")
        self.c("old", "join", "--agent", "old", "--human", "tek")
        self.c("old", "claim", "a.py", "-m", "wip")
        self.c("z", "claim", "a.py", "-m", "waiting")
        self.c("old", "task", "add", "T1: wip"); self.c("old", "task", "take", "T1")
        self.c("new", "join", "--agent", "new", "--human", "tek")
        out = self.c("new", "adopt", "old", "-m", "my laptop died")
        self.assertIn("claim C", out)
        self.assertIn("task T1", out)
        out = self.c("new", "release", "-m", "done")
        self.assertIn("next holder: z", out)

    def test_install_writes_settings_and_claude_md(self):
        d = self.agent("i", join=False)
        self.c("i", "install")
        s = json.load(open(os.path.join(d, ".claude", "settings.json")))
        self.assertIn("SessionStart", s["hooks"])
        self.assertIn("coord:begin", open(os.path.join(d, "CLAUDE.md")).read())
        self.c("i", "install")  # idempotent
        s = json.load(open(os.path.join(d, ".claude", "settings.json")))
        self.assertEqual(len(s["hooks"]["PostToolUse"]), 1)

    def test_dead_reviewer_reassign_and_wait_interrupted_by_review(self):
        # round-3 failure: hotfix review assigned to an agent whose session had ended
        self.agent("kai"); self.agent("nora"); self.agent("lena")
        live = dict(os.environ, COORD_LIVE="3")
        run = lambda who, *a: subprocess.run([sys.executable, COORD, *a],
                                             cwd=os.path.join(self.tmp, who),
                                             capture_output=True, text=True, env=live)
        self.c("kai", "claim", "x.py", "-m", "money")
        out = self.c("lena", "claim", "x.py", "-m", "orders")      # lena is queued behind kai
        qid = out.split()[1].rstrip(":")
        out = self.c("kai", "review", "request", "--to", "nora", "-m", "hotfix")
        rid = out.split()[0]
        time.sleep(4)                       # nora goes silent -> stale; lena stays live
        run("lena", "status", "working")
        p = run("kai", "inbox")
        self.assertIn("REVIEWER STALE", p.stdout)
        p = run("kai", "review", "reassign", rid)
        self.assertIn("reassigned to lena", p.stdout, p.stderr)
        # lena blocks on her queued claim, but the review request interrupts the wait
        p = run("lena", "wait", "--claim", qid, "--timeout", "20s", "--interval", "1s")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("REVIEW REQUEST", p.stdout)
        self.assertIn("returned early", p.stdout)

    def test_waiting_agent_stays_live(self):
        self.agent("w"); self.agent("x")
        env = dict(os.environ, COORD_LIVE="6")
        time.sleep(3)   # w silent for > LIVE/3
        subprocess.run([sys.executable, COORD, "wait", "--timeout", "4s", "--interval", "1s"],
                       cwd=os.path.join(self.tmp, "w"), capture_output=True, text=True, env=env)
        p = subprocess.run([sys.executable, COORD, "board"], cwd=os.path.join(self.tmp, "x"),
                           capture_output=True, text=True, env=env)
        self.assertIn("agent w [active]", p.stdout)

    def test_spec_travels_through_handoff_into_review(self):
        # round-3 failure: handoff note summarised away the request body shape
        self.agent("milo"); self.agent("pia"); self.agent("lena")
        spec = 'POST /checkout body {"items": {sku: qty}, "region": .., "coupon": optional} -> 201'
        self.c("milo", "task", "add", "API-2: checkout route", "--spec", spec)
        self.c("milo", "task", "drop", "API-2", "--to", "any", "-m", "skeleton on main")
        out = self.c("pia", "task", "take", "API-2")
        self.assertIn('{"items": {sku: qty}', out)
        self.c("pia", "review", "request", "--to", "lena", "--task", "API-2", "-m", "routes")
        self.assertIn("ACCEPTANCE CRITERIA (API-2)", self.c("lena", "inbox"))

    def test_last_agent_standing_self_review_is_audited(self):
        self.agent("omar"); self.agent("nora")
        self.assertIn("self-review is only allowed",
                      self.c("omar", "review", "request", "--self", "-m", "x", ok=False))
        env = dict(os.environ, COORD_LIVE="2")
        time.sleep(3)
        run = lambda *a: subprocess.run([sys.executable, COORD, *a], capture_output=True,
                                        text=True, env=env, cwd=os.path.join(self.tmp, "omar"))
        p = run("review", "request", "--to", "nora", "-m", "release")
        self.assertIn("not live", p.stderr)
        p = run("review", "request", "-m", "release")
        self.assertIn("--self", p.stderr)
        p = run("review", "request", "--self", "-m", "release: tests green, diff is version+changelog")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("release: tests green", run("digest").stdout.split("## Decisions")[0])

    # ---- round-4 (novice team) safety nets ----------------------------
    def seed_main(self, name):
        d = os.path.join(self.tmp, name)
        g = ["git", "-c", "user.name=s", "-c", "user.email=s@x"]
        with open(os.path.join(d, "index.html"), "w") as f:
            f.write("<html></html>\n")
        sh("git", "add", "index.html", cwd=d)
        sh(*g, "commit", "-qm", "seed", "--no-verify", cwd=d)
        sh("git", "push", "-q", "origin", "HEAD:main", cwd=d)

    def test_release_refuses_work_not_on_main_then_strands_it(self):
        # day 2: casey committed the Shop page locally, released and left
        d = self.agent("casey"); self.agent("sam")
        self.seed_main("casey")
        self.c("casey", "claim", "shop.html", "-m", "shop page")
        with open(os.path.join(d, "shop.html"), "w") as f:
            f.write("<h1>Shop</h1>\n")
        sh("git", "add", "shop.html", cwd=d)
        sh("git", "-c", "user.name=c", "-c", "user.email=c@x", "commit", "-qm", "shop", cwd=d)
        out = self.c("casey", "release", "-m", "done", ok=False)
        self.assertIn("NOT ON MAIN", out)
        self.assertIn("NOT ON MAIN", self.c("casey", "leave", ok=False))
        self.c("casey", "release", "--unmerged", "waiting for my human", "-m", "shop page")
        self.assertIn("UNFINISHED STRANDED-", self.c("sam", "inbox"))
        p = sh("git", "ls-remote", "origin", "refs/heads/wip/casey", cwd=d)
        self.assertTrue(p.stdout.strip(), "wip branch must exist")

    def test_hook_autosaves_uncommitted_work_to_wip(self):
        # a laptop that dies mid-edit must not lose the edit
        d = self.agent("riley", join=False)
        self.seed_main("riley")
        self.hook("riley", ["session-start"], {"session_id": "r1", "cwd": d})
        with open(os.path.join(d, "banner.css"), "w") as f:
            f.write(".sale{color:red}\n")
        self.hook("riley", ["hook"], {"session_id": "r1", "cwd": d,
                                      "hook_event_name": "PostToolUse"},
                  env={"COORD_HOOK_INTERVAL": "0"})
        name = json.load(open(os.path.join(d, ".git", "coord", "identity.json")))["agent"]
        p = sh("git", "show", f"origin/wip/{name}:banner.css", cwd=d, check=False)
        if p.returncode:
            sh("git", "fetch", "-q", "origin", f"wip/{name}", cwd=d)
            p = sh("git", "show", "FETCH_HEAD:banner.css", cwd=d)
        self.assertIn(".sale", p.stdout)
        self.assertEqual(sh("git", "status", "--porcelain", cwd=d).stdout.strip(), "?? banner.css",
                         "autosave must not touch the working tree or index")

    def test_abandoned_claim_becomes_orphan_task_for_the_fixer(self):
        # day 2: riley died holding the sale-banner claim; sam's audit never saw it
        self.agent("riley"); self.agent("sam")
        self.c("riley", "claim", "sale-banner.js", "--ttl", "2s", "-m", "50% off sale banner")
        time.sleep(3)
        out = self.c("sam", "board")
        self.assertIn("ORPHAN-C", out)
        out = self.c("sam", "join", "--agent", "sam")
        self.assertIn("UNFINISHED WORK LEFT BY OTHERS", out)
        self.assertIn("sale banner", out)
        tid = [w for w in out.split() if w.startswith("ORPHAN-")][0]
        self.assertIn("wip/riley", self.c("sam", "task", "take", tid))

    def test_messages_and_inbox(self):
        self.agent("a"); self.agent("b"); self.agent("c")
        self.c("a", "say", "use Decimal", "--to", "b", "--kind", "contract")
        self.assertIn("use Decimal", self.c("b", "inbox"))
        self.assertNotIn("use Decimal", self.c("c", "inbox"))
        self.assertIn("inbox empty", self.c("b", "inbox"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
