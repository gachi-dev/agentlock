# agentlock

Stops multiple AI coding agents from editing the same file at once and silently destroying each other's work.

Zero dependencies, Python 3.8+, a single 771-line file. [한국어](README.md)

The tool in this repository is **MIT and free**. It is fine on its own.
If you also need discipline around deploys, approvals, and completion reports, there is a paid **agentlock pro** — [plans and pricing](https://gachi-dev.github.io/agentlock/) · [what's different](#what-this-tool-does-not-enforce)

---

## Sound familiar?

You open two terminals. The left agent gets "refactor the payment API." The right agent gets "add payment validation." Both look like they're doing fine.

Fifteen minutes in, the left agent rewrites `src/payment.ts` from scratch and saves.
Three minutes later, the right agent appends validation logic to **the copy it read twenty minutes ago** and saves the same file.

Fifteen minutes of work is gone. And:

- **git won't tell you.** Nothing is committed yet, so there's no conflict. The last write simply wins.
- **The right agent doesn't know either.** It has no way to see what it just overwrote.
- **You don't know.** Both agents report "done."
- You find out three days later, digging through `git log` because payments are behaving strangely.

Run three or more agents and this stops being occasional. The odds compound with every agent you add.

agentlock enforces exactly one rule:

> **Declare the files you're about to touch, and don't touch files someone else declared.**

## It isn't just me

This is already on the record.

**Anthropic's own documentation admits it.** The Claude Code Agent Teams page says:

> Two teammates editing the same file leads to overwrites. Break the work so each teammate owns a different set of files.

The problem is acknowledged; the remedy offered is manual discipline. This tool is that discipline moved into code.

The same page also says this:

> Task claiming uses **file locking** to prevent race conditions when multiple teammates try to claim the same task simultaneously.

**Locking to claim a task. No locking to edit the file.** That gap is exactly where this tool sits.

There are incident reports too.

| Where | What happened |
|---|---|
| [claude-code #55586](https://github.com/anthropics/claude-code/issues/55586) | Spawning one teammate produced 151 instances that made **12,974 file edits** to the same codebase simultaneously |
| [openai/codex #10681](https://github.com/openai/codex/issues/10681) | One agent reverting another agent's changes — even after being told "Do NOT change any files you did not touch" |

## How this differs from git worktrees

Worktrees are the usual answer to this problem. **If your agents work on separate branches, worktrees are the right tool and this does not replace them.**

There is a place worktrees structurally cannot reach.

```console
$ git worktree add ../work-b feature-auth
fatal: 'feature-auth' is already checked out at '/path/work-a'
```

**Git refuses to check out the same branch twice.** Branch metadata is shared across worktrees, and there is no way around it. So "several agents, one branch" is outside what worktrees can do.

And a shared checkout is already common:

- Claude Code Agent Teams and subagents
- Several people on one repository, each running their own agent
- Agents running in CI

That is where this fits. You can run it alongside worktrees; they don't interfere.

| | git worktree | agentlock |
|---|---|---|
| Working on separate branches | **use this** | not needed |
| Several agents, one branch | git refuses | **use this** |
| When you learn about the conflict | at merge time, after both did the work | before either starts |
| Runtime, port and database isolation | no | no |
| Semantic conflicts | not caught | not caught |

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/gachi-dev/agentlock/main/agentlock.py -o /usr/local/bin/agentlock
chmod +x /usr/local/bin/agentlock
```

Or use the installer, which also checks your PATH:

```bash
curl -fsSL https://raw.githubusercontent.com/gachi-dev/agentlock/main/install.sh | sh
```

Python 3.8 or newer is the only requirement. No pip install, no third-party packages, no daemon.
Cloning the repo and copying `agentlock.py` alone works exactly the same.

## 60-second start

Everything below is copied verbatim from a real run with `AGENTLOCK_LANG=en`. (Demo repo lives at `/tmp/myapp`.)

**1. Set up the repo**

```console
$ agentlock init
Ready: /tmp/myapp/.agentlock
Commit locks.json and audit.jsonl. This only works if the whole team sees them.
```

**2. Claim files before you start working**

```console
$ agentlock claim src/payment.ts src/order.ts -a agent-a -t 30m --note "payment API refactor"
CLAIMED  src/payment.ts  (30m)
CLAIMED  src/order.ts  (30m)
```

**3. Another agent trying the same file gets blocked** (exit code 1)

```console
$ agentlock claim src/payment.ts -a agent-b -t 20m
Cannot claim it. Another agent is working on it.
  src/payment.ts
    └ agent-a is holding src/payment.ts (started 0s ago, 29m left)
      Note: payment API refactor

Wait for them to finish, or start on another file.
You can take it over with --force, but it goes in the audit log.
```

**4. See who's holding what**

```console
$ agentlock status
1 agents working

  agent-a  (2 files)
    ● src/order.ts  started 0s ago / 29m left
        payment API refactor
    ● src/payment.ts  started 0s ago / 29m left
        payment API refactor
```

**5. Install the commit hook so human slips get caught too**

```console
$ agentlock install-hook
Installed: /tmp/myapp/.git/hooks/pre-commit
Commits that include someone else's claimed files will now stop.
Give each agent its own AGENT_NAME environment variable.
  e.g.  export AGENT_NAME=codex
```

```console
$ AGENT_NAME=agent-b git commit -m "add payment validation"
Commit stopped. It includes files someone else is holding.
  src/payment.ts  ←  agent-a (src/payment.ts, 29m left)

What to do
  1. Wait for them to finish
  2. Unstage that file and commit the rest
  3. If you truly must take it over:  agentlock claim <path> -a agent-b --force
```

**6. Release when you're done**

```console
$ agentlock release -a agent-a --all
RELEASED  src/order.ts
RELEASED  src/payment.ts
```

**7. Everything that happened is on record**

```console
$ agentlock log
2026-08-14 15:22:44  CLAIM  agent-a      src/payment.ts
2026-08-14 15:22:44  CLAIM  agent-a      src/order.ts
2026-08-14 15:22:44  RELEASE  agent-a      src/order.ts
2026-08-14 15:22:44  RELEASE  agent-a      src/payment.ts
```

## Interface language

Messages come out in English or Korean. Set `AGENTLOCK_LANG` to `en` or `ko`:

```bash
export AGENTLOCK_LANG=en
```

Leave it unset and the tool follows your environment. A Korean locale gets Korean; anything else gets English. Lines with no translation fall back to the original, so nothing breaks if you edit the table. The table itself is the `EN` dict at the bottom of `agentlock.py` — change the wording to match how your team talks and the tool keeps working.

What gets written to `locks.json` and `audit.jsonl` is the same in either language, so two people on different settings still share one set of records.

## Commands

| Command | What it does | Common options |
|---|---|---|
| `init` | Prepare `.agentlock/` in the current repo | — |
| `claim <path…>` | Claim files (declare you're starting work) | `-a` agent name (required), `-t` TTL (default `30m`), `-n` note, `--force` take over |
| `release [path…]` | Release locks | `-a` (required), `--all` everything you hold, `--force` |
| `status` | Who's holding what right now | `--json` |
| `check [path…]` | Pre-commit check. With no paths, inspects git staged files | `-a` (required) |
| `who <path>` | Who holds this file | — |
| `log` | Audit log | `-n` line count (default 30), `-a` filter by agent |
| `install-hook` | Install the git `pre-commit` hook | `--force` overwrite an existing hook |

Global: `-V/--version`, `-h/--help`. Color is only enabled on a TTY and respects `NO_COLOR`.

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Refused because of a lock conflict (`claim`, `check`, `release`), or a hook already exists |
| `2` | Bad argument (`-t abc`), or not a git repo |
| `130` | Ctrl-C |

An agent wrapper only needs to read the `claim` exit code. Anything other than 0 means don't touch that file.

### What you can claim

Not just single paths.

```bash
agentlock claim src/payment.ts        -a a1    # a file
agentlock claim src/api               -a a1    # a directory -> covers everything under it
agentlock claim 'src/*.ts'            -a a1    # a glob (fnmatch)
agentlock claim src/a.ts src/b.ts     -a a1    # several at once
```

A directory lock covers every file beneath it. If you hold `src/api`, nobody else can claim `src/api/routes.ts`. It blocks in the other direction too — if someone holds `src/api/routes.ts`, you can't claim all of `src/api`.

Globs follow Python's `fnmatch` rules, so `*` also crosses `/`. `src/*.ts` will match `src/api/routes.ts`. If you don't want that reach, spell out the paths.

TTLs look like `30m`, `2h`, `90s`, `1d`. **A bare number means minutes** — `-t 45` is 45 minutes.

### Re-claiming as the same agent renews

```console
$ agentlock claim src/payment.ts -a agent-a -t 15m
RENEWED  src/payment.ts  (15m)
```

Your own locks never count as conflicts. For long jobs, just call the same command again periodically. `claimed_at` (when you first took it) is preserved; only the expiry moves.

## Three design decisions

### 1. Locks have a TTL — because agents die

Agents die more often and far more quietly than people do. Context overflow, rate limits, a user hitting Ctrl-C, someone closing the terminal window. In none of those cases does `release` ever run.

Without a TTL that file is locked **forever**. And that is exactly how a locking system actually dies: everyone starts muttering "oh, another ghost lock" and reflexively appending `--force`, and from that moment the lock prevents nothing at all.

So the default is 30 minutes, and expired locks are swept on the next command. There's no cleanup daemon.

```console
$ agentlock status
Nothing is being held.
(1 expired lock was released automatically)
```

> No files held. (1 lock expired and was released automatically.)

The short TTL is a deliberate trade. Work that runs past 30 minutes has to call `claim` again to extend. In exchange, ghost locks never pile up.

### 2. The audit log is append-only — because after an incident you need the sequence, not the current state

`locks.json` only knows about **now**. But when something goes wrong, the question isn't "who's holding this file" — it's **"when and why did my code disappear?"**

`.agentlock/audit.jsonl` only ever appends, one event per line. There is no code path that edits or deletes it.

```json
{"ts": "2026-08-14T01:29:21+00:00", "action": "claim", "pid": 11921, "path": "src/payment.ts", "agent": "agent-a", "ttl": 1800, "note": "payment API refactor"}
{"ts": "2026-08-14T01:30:33+00:00", "action": "release", "pid": 11934, "path": "src/payment.ts", "agent": "agent-a"}
```

Five event types are recorded: `claim`, `renew`, `release`, `expire`, `steal`. `steal` (a forced takeover) is the one that matters most.

```console
$ agentlock claim src/api/routes.ts -a a2 -t 10m --force
Taken by force: src/api (a1 → a2)
CLAIMED  src/api/routes.ts  (10m)

$ agentlock log
2026-08-14 01:25:59  CLAIM   a1           src/api
2026-08-14 01:25:59  CLAIM   a2           src/api/routes.ts
2026-08-14 01:25:59  STEAL   a2           src/api  (from a1)
```

`--force` isn't blocked, but it always leaves a trace. If there's no escape hatch when someone is in a hurry, people stop using the tool entirely. Better to allow the override and make "who overrode this, and when?" a 30-second lookup.

It parses directly with `jq`:

```bash
jq -r 'select(.action=="steal")' .agentlock/audit.jsonl
```

### 3. `locks.json` gets committed to git — because a lock only you can see isn't a lock

Here's the `.agentlock/.gitignore` that `init` writes:

```gitignore
.guard
*.tmp
*.corrupt
```

Only the inter-process guard file and temp files are excluded. **`locks.json` and `audit.jsonl` are meant to be committed.** That's on purpose.

A gitignored lock only exists on your machine. But agents don't live on one machine. They run in CI, on a teammate's laptop, in a remote session. Commit the state and everyone who pulls sees the same picture.

Three things fall out of that:

- Locks become reviewable. A PR diff makes "why did this agent claim all of `src/`?" visible.
- The audit log sits on the same timeline as your commit history. "Who force-took what right before this commit?" is immediately answerable.
- `locks.json` itself can conflict. That's the safe failure. **git stopping loudly** beats file contents vanishing quietly, and since keys are per-path, most merges resolve automatically.

Within a single machine, `.agentlock/.guard` handles mutual exclusion. It's an `O_EXCL` spinlock, so it behaves identically on Linux, macOS, and Windows, and a guard left behind by a dead process is reclaimed after 30 seconds. `locks.json` is written to a temp file, `fsync`ed, then swapped in with `os.replace`, so a crash mid-write never leaves a half-written file.

## Using it for real

### Give every agent a distinct name

The hook reads the `AGENT_NAME` environment variable, falls back to `git config user.name`, and finally to `unknown`. If every agent ends up as `unknown`, they can't be told apart and nothing gets blocked.

```bash
# agent A's terminal
export AGENT_NAME=agent-a

# agent B's terminal
export AGENT_NAME=agent-b
```

### Claim narrowly, expire quickly

`agentlock claim . -a me -t 8h` is valid syntax, but locking the whole repo for eight hours isn't a lock — it's a road closure. Once other agents can't do anything, people start passing `--force` by default.

Claiming only the files you'll actually edit, for only as long as you'll actually need, works far better.

### What running it taught us

This came out of an environment where eight kinds of agents worked on one repository for 55 days without interruption. At that scale the thing that actually caused problems wasn't the locking algorithm — it was these two:

- Ghost locks (an agent dies without calling release) — solved by TTL
- Nobody knowing who used `--force`, or when — solved by the audit log

Which is why this tool has no queueing, no priorities, and no wait notifications. It didn't need them.

## FAQ

### How is this different from a file lock (`flock`, `.lock` files)?

**They protect different windows.** An OS file lock is taken when a process opens a file descriptor, and the kernel drops it the instant the process dies. But coding agents don't hold files open. They **read, close, think for several minutes, then reopen and write.** The dangerous window is those minutes while the file is closed, and `flock` doesn't cover it at all.

agentlock locks **intent**, not a file descriptor. You're declaring "I'm going to edit this for the next 30 minutes," which stays true whether or not the file is open.

The rest of the differences:

| | OS file lock | agentlock |
|---|---|---|
| Valid while | the fd is open | the declared TTL lasts |
| Who holds it | hard to tell | `agentlock who` |
| History | none | append-only audit log |
| Visible on other machines | no | yes, shared via git |
| Enforcement | kernel-enforced | advisory (backed by the hook) |

That last row is the honest limitation. agentlock is an **advisory** lock. If you skip `agentlock claim` and just edit the file in `vim`, nothing stops you. That's what `install-hook` is for: one more filter at commit time, and in practice the real last line of defense.

### Can a team of humans use it?

Yes. Put people's names where agent names go and it works unchanged.

```bash
agentlock claim src/checkout/ -a minsu -t 2h --note "checkout flow rework, PR #412"
```

It's especially useful for:

- Pinning "don't touch this directory right now" into the repo instead of into Slack, during a large refactor
- Signaling who's regenerating machine-generated files (migrations, schemas, lockfiles)
- Mixed human-and-agent work on the same repo — which is honestly where most accidents happen

People work in much longer stretches than agents, so give humans generous TTLs. And this doesn't replace a branching strategy; it's for when you're working on the same branch simultaneously.

### How do I get agents to call this automatically?

Put this in the agent's system prompt (or in the agent instruction file at your repo root):

```markdown
## File lock rules (mandatory)

Multiple agents work in this repository. Always claim a lock before modifying a file.

1. Before creating, editing, or deleting any file, run:
       agentlock claim <path…> -a <my_agent_name> -t 30m --note "<one line: what you're doing>"

2. If the exit code is not 0, do NOT modify that file.
   The output shows who holds it. Report that to the user as-is, then either
   work on unlocked files or stop.
   Never use --force unless the user explicitly instructs you to.

3. If the work will take longer than 30 minutes, run the same claim command again to extend.

4. Release immediately when you're done editing:
       agentlock release <path…> -a <my_agent_name>
   At the end of a session:  agentlock release -a <my_agent_name> --all

5. To see what others hold:  agentlock status
   To check one file:        agentlock who <path>
```

Pin `<my_agent_name>` to something distinct per agent. If two agents share a name, they'll be treated as the same agent and the conflict check will simply pass.

One practical note: agents forget rules. Don't rely on the prompt alone — install the hook too. The prompt reduces accidents; the hook keeps accidents from reaching a commit.

### What if the lock file gets corrupted?

If `locks.json` isn't valid JSON, it isn't silently ignored. It's moved to `locks.json.corrupt`, a warning is printed, and state restarts empty — silent failure is the most dangerous outcome here.

```
Warning: the lock file was corrupt, so it was moved to <repo>/.agentlock/locks.json.corrupt and started fresh.
```

> Warning: the lock file was corrupt, so it was moved to `<repo>/.agentlock/locks.json.corrupt` and state was reset.

The broken file is kept as `.corrupt` rather than deleted, so you can open it later and see what happened.

### A stale `.agentlock/.guard` is blocking my commands

It's reclaimed automatically after 30 seconds. If you can't wait, delete the path shown in the message.

```
Another process is holding .agentlock. If it persists, delete <path>/.agentlock/.guard.
```

> Another process is holding .agentlock. If this persists, delete `<path>/.agentlock/.guard`.

### I already have a pre-commit hook

`install-hook` refuses to overwrite it and exits with code 1.

```
A pre-commit hook already exists: <path>/.git/hooks/pre-commit
Use --force to overwrite it.
```

> A pre-commit hook already exists at `<path>/.git/hooks/pre-commit`. Use `--force` to overwrite.

You can just add one line to your existing hook instead:

```sh
agentlock check -a "${AGENT_NAME:-$(git config user.name)}" || exit 1
```

With no paths given, it reads staged files from `git diff --cached` on its own.

## Tests

```bash
./test_agentlock.sh
```

It builds a throwaway repo under `/tmp` and verifies claim/deny/release, TTL expiry, directory locks, globs, `--force` audit records, pre-commit blocking, and zero lock loss across 20 concurrent processes — then cleans up after itself.

## What this tool does not enforce

agentlock is one of nine operating disciplines — number 3, declaring work before you start — moved into code. Those nine are what was left after running eight kinds of agents on one repository for 55 days:

1. Fixed role separation
2. File ownership and interface contracts
3. **Declaring work before you start** — the one this tool enforces
4. A single deployer
5. Handover when someone is unavailable
6. Pinned identity prompts
7. Verification evidence required in completion reports
8. Approval gates
9. An append-only ledger

The other eight are not enforced here. Shipping the same deploy twice, a completion report nobody verified, picking up work after a conversation window dies — number 3 stops none of them.

If you need that part too, it is kept separate from this repo.

**agentlock pro** — all nine enforced in code. One `setup` run assigns roles, file ownership, and the deploy owner, and installs the git hooks. Committing a file owned by someone else is blocked, a completion report that leaves the "not verified" section empty is rejected, a deploy only runs when the designated person has human approval, and records are hash-chained, so editing one afterwards tells you exactly which line was tampered with. **A document kit ships with it** — the full text of all nine disciplines, 3 agent identity prompt templates, 5 forms, an incident procedure for when work does collide, and an adoption checklist. Nothing to buy separately.

Pro is a **subscription**. There are three plans, all shipping the same files; only the permitted scope of use differs.

| | Solo | Team | Business |
|---|---|---|---|
| Monthly | $29 | $79 | $199 |
| Yearly (20% off) | $278 | $758 | $1,910 |
| People covered | 1 | 10 on one repository | unlimited |
| Shared servers and CI | no | yes | yes |
| Commit to an internal repository | no | yes | yes |

Integrating the source into another piece of software is in none of the plans. Ask separately if you need it.

Pro commands run against a license key. The server is contacted once a day, keeps working for 14 days when it can't be reached, and what gets sent is the key, an activation identifier, and the machine label you chose. No source code and no file listings leave your machine in any form.

**English and Korean are both supported.** Set the interface language with `AGENTLOCK_LANG`; leave it unset and it follows your environment. The manual, quick start, license guide and the full document kit all ship in both languages.

Plans and pricing are here — **https://gachi-dev.github.io/agentlock/**

One thing said plainly first: the free tool is enough for a lot of setups. If you're running two or three agents on your own, number 3 alone stops most of the accidents, and you don't need more than that. Pro is worth a look once you have to put discipline around deploys and approvals as well.

## License

MIT. See [LICENSE](LICENSE).
