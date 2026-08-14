#!/usr/bin/env python3
# agentlock — 여러 AI 에이전트가 같은 파일을 동시에 고쳐 작업이 날아가는 걸 막는 파일 단위 락
"""
agentlock - file ownership locks for multi-agent coding sessions.

Claude Code, Codex, Cursor 같은 코딩 에이전트를 두 개 이상 동시에 돌리면
결국 같은 파일을 동시에 고치는 순간이 옵니다. 늦게 끝난 쪽이 먼저 끝난 쪽을
통째로 덮어쓰고, 덮어쓴 쪽도 덮어썼다는 사실을 모릅니다.

이 도구는 규칙 하나만 강제합니다.
    "작업할 파일을 먼저 선언하고, 남이 선언한 파일은 건드리지 않는다."

의존성 없음. 파이썬 3.8 이상. 단일 파일.

Usage:
    agentlock claim src/api.ts -a codex -t 30m
    agentlock status
    agentlock check src/api.ts -a claude
    agentlock release src/api.ts -a codex
    agentlock install-hook

License: MIT
"""

from __future__ import annotations

import argparse
import errno
import fnmatch
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

__version__ = "1.1.0"

# ---------------------------------------------------------------------------
# 화면에 나가는 글의 언어 / Interface language
# ---------------------------------------------------------------------------
# AGENTLOCK_LANG 으로 직접 정합니다 (ko / en). 정하지 않으면 시스템 로케일을 보고,
# 한국어 환경이면 한국어로 둡니다. 쓰던 분이 갑자기 영어 화면을 보지 않게 하려는 것입니다.
# 번역이 없는 문장은 원문이 그대로 나옵니다. 번역표가 비어 있어도 멈추지 않습니다.


def _pick_lang() -> str:
    want = (os.environ.get("AGENTLOCK_LANG") or "").strip().lower()
    if want.startswith("en"):
        return "en"
    if want.startswith("ko"):
        return "ko"
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = (os.environ.get(key) or "").strip().lower()
        if not val:
            continue
        if val.startswith("ko"):
            return "ko"
        if val in ("c", "posix", "c.utf-8"):
            continue          # 로케일을 안 정한 것이지 영어라는 뜻은 아니다
        return "en"
    return "ko"


LANG = _pick_lang()


def _t(s: str) -> str:
    """화면에 나갈 문장 하나를 지금 언어로 바꿉니다. 번역이 없으면 원문 그대로."""
    if LANG == "ko":
        return s
    return EN.get(s, s)


STATE_DIR = ".agentlock"
LOCKS_FILE = "locks.json"
AUDIT_FILE = "audit.jsonl"
GUARD_FILE = ".guard"

DEFAULT_TTL = 1800  # 30분. 에이전트가 죽어도 락이 영원히 남지 않도록.
GUARD_TIMEOUT = 5.0
GUARD_STALE = 30.0

C_RED = "\033[31m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_OFF = "\033[0m"


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def paint(text: str, color: str) -> str:
    return f"{color}{text}{C_OFF}" if _color_enabled() else text


# ---------------------------------------------------------------------------
# 저장소
# ---------------------------------------------------------------------------


def find_root(start: str | None = None) -> str:
    """.agentlock 또는 .git 이 있는 상위 디렉터리를 프로젝트 루트로 본다."""
    cur = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(cur, STATE_DIR)):
            return cur
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.abspath(start or os.getcwd())
        cur = parent


def state_dir(root: str) -> str:
    return os.path.join(root, STATE_DIR)


class Guard:
    """상태 파일을 고칠 때 쓰는 프로세스 간 배타 락.

    O_EXCL 스핀락이라 윈도우/맥/리눅스에서 똑같이 동작한다.
    죽은 프로세스가 남긴 가드는 GUARD_STALE 초 후 자동 회수한다.
    """

    def __init__(self, root: str):
        self.path = os.path.join(state_dir(root), GUARD_FILE)
        self.fd: int | None = None

    def __enter__(self) -> "Guard":
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.time() + GUARD_TIMEOUT
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                try:
                    age = time.time() - os.path.getmtime(self.path)
                    if age > GUARD_STALE:
                        os.unlink(self.path)
                        continue
                except FileNotFoundError:
                    continue
                if time.time() > deadline:
                    raise SystemExit(
                        paint(
                            _t('다른 프로세스가 {0} 를 잡고 있습니다. 계속 이러면 {1} 를 지우세요.').format(STATE_DIR, self.path),
                            C_RED,
                        )
                    )
                time.sleep(0.05)

    def __exit__(self, *_exc) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def load_locks(root: str) -> dict:
    path = os.path.join(state_dir(root), LOCKS_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        # 상태 파일이 깨졌으면 조용히 무시하지 않는다. 조용한 실패가 제일 위험하다.
        backup = path + ".corrupt"
        os.replace(path, backup)
        print(
            paint(_t('경고: 락 파일이 깨져서 {0} 로 옮기고 새로 시작합니다.').format(backup), C_YELLOW),
            file=sys.stderr,
        )
        return {}
    return data if isinstance(data, dict) else {}


def save_locks(root: str, locks: dict) -> None:
    d = state_dir(root)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, LOCKS_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(locks, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)  # 원자적 교체. 중간에 죽어도 반쪽짜리 파일이 안 남는다.


def audit(root: str, action: str, **fields) -> None:
    """append-only 감사 로그. 되돌리지 않고 쌓기만 한다."""
    d = state_dir(root)
    os.makedirs(d, exist_ok=True)
    record = {"ts": now_iso(), "action": action, "pid": os.getpid()}
    record.update(fields)
    with open(os.path.join(d, AUDIT_FILE), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_ttl(text: str) -> int:
    """30m, 2h, 90s, 45 를 초로. 숫자만 오면 분으로 본다."""
    text = str(text).strip().lower()
    if not text:
        raise ValueError(_t("빈 TTL"))
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text[-1] in units:
        value, mult = text[:-1], units[text[-1]]
    else:
        value, mult = text, 60
    try:
        n = float(value)
    except ValueError:
        raise ValueError(_t('TTL 형식이 잘못됐습니다: {0}').format(text))
    if n <= 0:
        raise ValueError(_t("TTL은 0보다 커야 합니다"))
    return int(n * mult)


def human_left(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds >= 3600:
        return _t('{0}시간 {1}분').format(seconds // 3600, (seconds % 3600) // 60)
    if seconds >= 60:
        return _t('{0}분').format(seconds // 60)
    return _t('{0}초').format(seconds)


def human_ago(iso: str) -> str:
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return "?"
    delta = (datetime.now(timezone.utc).astimezone() - then).total_seconds()
    return human_left(delta) + _t(" 전")


def norm(root: str, path: str) -> str:
    """저장은 항상 루트 기준 상대경로 + 슬래시로 통일."""
    if any(ch in path for ch in "*?["):
        # lstrip("./") 을 쓰면 안 된다. 문자 집합을 지우기 때문에
        # ".github/*.yml" 이 "github/*.yml" 로 조용히 바뀐다.
        p = path.replace(os.sep, "/")
        while p.startswith("./"):
            p = p[2:]
        return p
    abs_p = os.path.abspath(os.path.join(os.getcwd(), path))
    try:
        rel = os.path.relpath(abs_p, root)
    except ValueError:
        rel = abs_p
    return rel.replace(os.sep, "/")


def prune(locks: dict, root: str) -> list[tuple[str, dict]]:
    """만료된 락을 걷어낸다. 걷어낸 목록을 돌려준다."""
    now = time.time()
    expired = []
    for path in list(locks):
        entry = locks[path]
        if entry.get("expires_at", 0) <= now:
            expired.append((path, entry))
            del locks[path]
    for path, entry in expired:
        audit(root, "expire", path=path, agent=entry.get("agent"))
    return expired


def covers(lock_path: str, target: str) -> bool:
    """lock_path 가 target 을 덮는지. 디렉터리와 글로브를 모두 본다."""
    if lock_path == target:
        return True
    if any(ch in lock_path for ch in "*?["):
        return fnmatch.fnmatch(target, lock_path)
    # 디렉터리 락은 하위 전부를 덮는다
    return target.startswith(lock_path.rstrip("/") + "/")


def conflicts(locks: dict, target: str, agent: str) -> list[tuple[str, dict]]:
    """target 을 잡으려 할 때 걸리는 남의 락 목록."""
    out = []
    for path, entry in locks.items():
        if entry.get("agent") == agent:
            continue
        if covers(path, target) or covers(target, path):
            out.append((path, entry))
    return out


# ---------------------------------------------------------------------------
# 명령
# ---------------------------------------------------------------------------


def cmd_claim(args) -> int:
    root = find_root()
    try:
        ttl = parse_ttl(args.ttl)
    except ValueError as exc:
        print(paint(str(exc), C_RED), file=sys.stderr)
        return 2

    with Guard(root):
        locks = load_locks(root)
        prune(locks, root)

        targets = [norm(root, p) for p in args.paths]
        blocked: list[tuple[str, str, dict]] = []
        for t in targets:
            for lp, entry in conflicts(locks, t, args.agent):
                blocked.append((t, lp, entry))

        if blocked and not args.force:
            print(paint(_t("잡을 수 없습니다. 다른 에이전트가 작업 중입니다."), C_RED))
            for target, lock_path, entry in blocked:
                left = human_left(entry.get("expires_at", 0) - time.time())
                who = paint(entry.get("agent", "?"), C_BOLD)
                print(f"  {target}")
                print(
                    _t('    └ {0} 가 {1} 를 잡고 있음 ({2} 시작, {3} 남음)').format(who, lock_path, human_ago(entry.get('claimed_at', '')), left)
                )
                if entry.get("note"):
                    print(paint(_t('      메모: {0}').format(entry['note']), C_DIM))
            print()
            print(paint(_t("먼저 끝나기를 기다리거나, 다른 파일부터 작업하세요."), C_DIM))
            print(paint(_t("정말 넘겨받아야 하면 --force 를 쓰되 감사 로그에 남습니다."), C_DIM))
            return 1

        now = time.time()
        renewed, taken, stolen = [], [], []
        for t in targets:
            prev = locks.get(t)
            mine = bool(prev and prev.get("agent") == args.agent)

            # 이 경로를 덮고 있는 남의 락은 경우를 가리지 않고 전부 회수한다.
            # 상위 디렉터리 락과 하위 파일 락이 동시에 남으면
            # 같은 파일을 둘이 소유한 것으로 보이게 된다.
            for lp, entry in conflicts(locks, t, args.agent):
                stolen.append((lp, entry.get("agent")))
                locks.pop(lp, None)

            (renewed if mine else taken).append(t)

            locks[t] = {
                "agent": args.agent,
                "claimed_at": prev.get("claimed_at", now_iso()) if mine else now_iso(),
                "expires_at": now + ttl,
                "ttl": ttl,
                "note": args.note or (prev.get("note", "") if mine else ""),
                "pid": os.getpid(),
            }

        save_locks(root, locks)
        for t in taken:
            audit(root, "claim", path=t, agent=args.agent, ttl=ttl, note=args.note or "")
        for t in renewed:
            audit(root, "renew", path=t, agent=args.agent, ttl=ttl)
        for path, victim in stolen:
            audit(root, "steal", path=path, agent=args.agent, taken_from=victim)

    for path, victim in stolen:
        print(paint(_t('강제 회수: {0} ({1} → {2})').format(path, victim, args.agent), C_YELLOW))
    for t in taken:
        print(paint(_t('확보  {0}').format(t), C_GREEN) + paint(f"  ({human_left(ttl)})", C_DIM))
    for t in renewed:
        print(paint(_t('연장  {0}').format(t), C_GREEN) + paint(f"  ({human_left(ttl)})", C_DIM))
    return 0


def cmd_release(args) -> int:
    root = find_root()
    with Guard(root):
        locks = load_locks(root)
        prune(locks, root)

        if args.all:
            targets = [p for p, e in locks.items() if e.get("agent") == args.agent]
        else:
            targets = [norm(root, p) for p in args.paths]

        if not targets:
            print(paint(_t("풀 락이 없습니다."), C_DIM))
            return 0

        released, denied = [], []
        for t in targets:
            entry = locks.get(t)
            if not entry:
                continue
            if entry.get("agent") != args.agent and not args.force:
                denied.append((t, entry.get("agent")))
                continue
            del locks[t]
            released.append(t)

        save_locks(root, locks)
        for t in released:
            audit(root, "release", path=t, agent=args.agent)

    for t, owner in denied:
        print(paint(_t('거부  {0} 는 {1} 의 락입니다').format(t, owner), C_RED))
    for t in released:
        print(paint(_t('해제  {0}').format(t), C_GREEN))
    return 1 if denied else 0


def cmd_status(args) -> int:
    root = find_root()
    with Guard(root):
        locks = load_locks(root)
        expired = prune(locks, root)
        if expired:
            save_locks(root, locks)

    if args.json:
        print(json.dumps(locks, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if not locks:
        print(paint(_t("잡혀 있는 파일이 없습니다."), C_DIM))
        if expired:
            print(paint(_t('({0}건이 만료되어 자동 해제됐습니다)').format(len(expired)), C_DIM))
        return 0

    by_agent: dict[str, list] = {}
    for path, entry in sorted(locks.items()):
        by_agent.setdefault(entry.get("agent", "?"), []).append((path, entry))

    print(paint(_t('작업 중인 에이전트 {0}').format(len(by_agent)), C_BOLD))
    for agent, items in sorted(by_agent.items()):
        print()
        print(_t('  {0}  ({1}개 파일)').format(paint(agent, C_BOLD), len(items)))
        for path, entry in items:
            left = entry.get("expires_at", 0) - time.time()
            mark = C_YELLOW if left < 300 else C_GREEN
            print(
                f"    {paint('●', mark)} {path}  "
                + paint(
                    _t('{0} 시작 / {1} 남음').format(human_ago(entry.get('claimed_at', '')), human_left(left)),
                    C_DIM,
                )
            )
            if entry.get("note"):
                print(paint(f"        {entry['note']}", C_DIM))
    if expired:
        print()
        print(paint(_t('{0}건이 만료되어 자동 해제됐습니다.').format(len(expired)), C_DIM))
    return 0


def cmd_check(args) -> int:
    """커밋 훅용. 남이 잡은 파일을 건드리면 1을 리턴한다."""
    root = find_root()
    with Guard(root):
        locks = load_locks(root)
        prune(locks, root)

    paths = args.paths or staged_files(root)
    if not paths:
        return 0

    problems = []
    for p in paths:
        t = norm(root, p)
        for lp, entry in conflicts(locks, t, args.agent):
            problems.append((t, lp, entry))

    if not problems:
        return 0

    print(paint(_t("커밋을 멈췄습니다. 남이 잡고 있는 파일이 섞였습니다."), C_RED))
    for target, lock_path, entry in problems:
        left = human_left(entry.get("expires_at", 0) - time.time())
        print(_t('  {0}  ←  {1} ({2}, {3} 남음)').format(target, paint(entry.get('agent', '?'), C_BOLD), lock_path, left))
    print()
    print(paint(_t("해결 방법"), C_BOLD))
    print(_t("  1. 상대가 끝낼 때까지 기다린다"))
    print(_t("  2. 해당 파일만 커밋에서 빼고 나머지를 올린다"))
    print(_t('  3. 정말 넘겨받아야 하면  agentlock claim <경로> -a {0} --force').format(args.agent))
    return 1


def staged_files(root: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


def cmd_who(args) -> int:
    root = find_root()
    with Guard(root):
        locks = load_locks(root)
        prune(locks, root)
    t = norm(root, args.path)
    found = [(lp, e) for lp, e in locks.items() if covers(lp, t)]
    if not found:
        print(paint(_t("아무도 안 잡고 있습니다."), C_GREEN))
        return 0
    for lp, entry in found:
        left = human_left(entry.get("expires_at", 0) - time.time())
        print(_t('{0}  ({1}, {2} 남음)').format(paint(entry.get('agent', '?'), C_BOLD), lp, left))
        if entry.get("note"):
            print(paint(f"  {entry['note']}", C_DIM))
    return 0


def cmd_log(args) -> int:
    root = find_root()
    path = os.path.join(state_dir(root), AUDIT_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        print(paint(_t("감사 로그가 아직 없습니다."), C_DIM))
        return 0

    icons = {
        "claim": paint(_t("확보"), C_GREEN),
        "renew": paint(_t("연장"), C_GREEN),
        "release": paint(_t("해제"), C_DIM),
        "expire": paint(_t("만료"), C_YELLOW),
        "steal": paint(_t("강제회수"), C_RED),
    }
    for line in lines[-args.number :]:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if args.agent and rec.get("agent") != args.agent:
            continue
        ts = rec.get("ts", "")[:19].replace("T", " ")
        action = icons.get(rec.get("action", ""), rec.get("action", ""))
        extra = ""
        if rec.get("taken_from"):
            extra = paint(_t('  ({0} 로부터)').format(rec['taken_from']), C_DIM)
        print(f"{paint(ts, C_DIM)}  {action}  {rec.get('agent', '?'):<12} {rec.get('path', '')}{extra}")
    return 0


# $AGENT_NAME 은 셸이 해석해야 하므로 파이썬 포맷팅을 쓰지 않는다.
HOOK = """#!/bin/sh
# installed by agentlock
AGENT="${AGENT_NAME:-$(git config user.name 2>/dev/null || echo unknown)}"
exec "__PYTHON__" "__SCRIPT__" check -a "$AGENT"
"""


def cmd_install_hook(args) -> int:
    root = find_root()
    hooks = os.path.join(root, ".git", "hooks")
    if not os.path.isdir(hooks):
        print(paint(_t("git 저장소가 아닙니다."), C_RED), file=sys.stderr)
        return 2

    target = os.path.join(hooks, "pre-commit")
    if os.path.exists(target) and not args.force:
        print(paint(_t('이미 pre-commit 훅이 있습니다: {0}').format(target), C_YELLOW))
        print(paint(_t("덮어쓰려면 --force 를 쓰세요."), C_DIM))
        return 1

    # 내용을 먼저 완성한다. 중간에 실패해서 빈 훅 파일이 남으면 커밋이 조용히 통과한다.
    body = HOOK.replace("__PYTHON__", sys.executable).replace(
        "__SCRIPT__", os.path.abspath(__file__)
    )
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(tmp, 0o755)
    os.replace(tmp, target)

    print(paint(_t('설치했습니다: {0}').format(target), C_GREEN))
    print(paint(_t("이제 남이 잡은 파일이 섞이면 커밋이 멈춥니다."), C_DIM))
    print(paint(_t("에이전트마다 AGENT_NAME 환경변수를 다르게 주세요."), C_DIM))
    print(paint(_t('  예)  export AGENT_NAME=codex'), C_DIM))
    return 0


def cmd_init(args) -> int:
    root = find_root()
    d = state_dir(root)
    os.makedirs(d, exist_ok=True)
    gitignore = os.path.join(d, ".gitignore")
    if not os.path.exists(gitignore):
        with open(gitignore, "w", encoding="utf-8") as fh:
            # 가드와 임시파일만 제외. 락과 감사로그는 공유해야 의미가 있다.
            fh.write(".guard\n*.tmp\n*.corrupt\n")
    print(paint(_t('준비됐습니다: {0}').format(d), C_GREEN))
    print(paint(_t("locks.json 과 audit.jsonl 은 커밋에 포함하세요. 팀이 같이 봐야 합니다."), C_DIM))
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentlock",
        description=_t("여러 AI 에이전트가 같은 파일을 동시에 고치는 사고를 막습니다."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_t("""예시
  agentlock init
  agentlock claim src/api.ts src/db.ts -a codex -t 30m --note "결제 API 리팩터링"
  agentlock status
  agentlock who src/api.ts
  agentlock check -a claude
  agentlock release -a codex --all
  agentlock install-hook
"""),
    )
    p.add_argument("-V", "--version", action="version", version=f"agentlock {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("init", help=_t("현재 저장소에 .agentlock 준비"))
    c.set_defaults(func=cmd_init)

    c = sub.add_parser("claim", help=_t("파일을 잡는다 (작업 시작 선언)"))
    c.add_argument("paths", nargs="+")
    c.add_argument("-a", "--agent", required=True, help=_t("에이전트 이름 (codex, claude, cursor …)"))
    c.add_argument("-t", "--ttl", default="30m", help=_t("유효 시간. 기본 30m"))
    c.add_argument("-n", "--note", default="", help=_t("무슨 작업인지 한 줄"))
    c.add_argument("--force", action="store_true", help=_t("남의 락을 강제로 회수 (감사 로그에 남음)"))
    c.set_defaults(func=cmd_claim)

    c = sub.add_parser("release", help=_t("락을 푼다"))
    c.add_argument("paths", nargs="*")
    c.add_argument("-a", "--agent", required=True)
    c.add_argument("--all", action="store_true", help=_t("내가 잡은 것 전부"))
    c.add_argument("--force", action="store_true")
    c.set_defaults(func=cmd_release)

    c = sub.add_parser("status", help=_t("지금 누가 뭘 잡고 있나"))
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_status)

    c = sub.add_parser("check", help=_t("커밋 전 검사. 남의 파일이 섞였으면 실패"))
    c.add_argument("paths", nargs="*", help=_t("비우면 git staged 파일을 본다"))
    c.add_argument("-a", "--agent", required=True)
    c.set_defaults(func=cmd_check)

    c = sub.add_parser("who", help=_t("이 파일 누가 잡고 있나"))
    c.add_argument("path")
    c.set_defaults(func=cmd_who)

    c = sub.add_parser("log", help=_t("감사 로그"))
    c.add_argument("-n", "--number", type=int, default=30)
    c.add_argument("-a", "--agent", default="")
    c.set_defaults(func=cmd_log)

    c = sub.add_parser("install-hook", help=_t("git pre-commit 훅 설치"))
    c.add_argument("--force", action="store_true")
    c.set_defaults(func=cmd_install_hook)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


# ---------------------------------------------------------------------------
# 영문 번역표 / English strings
# ---------------------------------------------------------------------------
# 왼쪽이 원문, 오른쪽이 영문입니다. 여기 없는 문장은 원문이 그대로 나옵니다.

EN = {
    ' 전': ' ago',
    '  1. 상대가 끝낼 때까지 기다린다': '  1. Wait for them to finish',
    '  2. 해당 파일만 커밋에서 빼고 나머지를 올린다': '  2. Unstage that file and commit the rest',
    '빈 TTL': 'empty TTL',
    'TTL은 0보다 커야 합니다': 'TTL must be greater than 0',
    '{0}초': '{0}s',
    '커밋을 멈췄습니다. 남이 잡고 있는 파일이 섞였습니다.': 'Commit stopped. It includes files someone else is holding.',
    '해결 방법': 'What to do',
    '확보': 'CLAIM',
    '연장': 'RENEW',
    '해제': 'RELEASE',
    '만료': 'EXPIRE',
    '강제회수': 'STEAL',
    '이제 남이 잡은 파일이 섞이면 커밋이 멈춥니다.': "Commits that include someone else's claimed files will now stop.",
    '에이전트마다 AGENT_NAME 환경변수를 다르게 주세요.': 'Give each agent its own AGENT_NAME environment variable.',
    '  예)  export AGENT_NAME=codex': '  e.g.  export AGENT_NAME=codex',
    'locks.json 과 audit.jsonl 은 커밋에 포함하세요. 팀이 같이 봐야 합니다.': 'Commit locks.json and audit.jsonl. This only works if the whole team sees them.',
    '여러 AI 에이전트가 같은 파일을 동시에 고치는 사고를 막습니다.': 'Stops several AI agents from editing the same file at once.',
    '예시\n  agentlock init\n  agentlock claim src/api.ts src/db.ts -a codex -t 30m --note "결제 API 리팩터링"\n  agentlock status\n  agentlock who src/api.ts\n  agentlock check -a claude\n  agentlock release -a codex --all\n  agentlock install-hook\n': 'Examples\n  agentlock init\n  agentlock claim src/api.ts src/db.ts -a codex -t 30m --note "payment API refactor"\n  agentlock status\n  agentlock who src/api.ts\n  agentlock check -a claude\n  agentlock release -a codex --all\n  agentlock install-hook\n',
    '현재 저장소에 .agentlock 준비': 'prepare .agentlock in the current repository',
    '파일을 잡는다 (작업 시작 선언)': 'claim files (declare work before you start)',
    '에이전트 이름 (codex, claude, cursor …)': 'agent name (codex, claude, cursor …)',
    '유효 시간. 기본 30m': 'how long the claim lasts. Default 30m',
    '무슨 작업인지 한 줄': 'one line on what you are doing',
    '남의 락을 강제로 회수 (감사 로그에 남음)': "take someone else's lock by force (recorded in the audit log)",
    '락을 푼다': 'release locks',
    '내가 잡은 것 전부': 'everything I hold',
    '지금 누가 뭘 잡고 있나': 'who is holding what right now',
    '커밋 전 검사. 남의 파일이 섞였으면 실패': "pre-commit check. Fails if someone else's files are included",
    '비우면 git staged 파일을 본다': 'with no paths, inspects git staged files',
    '이 파일 누가 잡고 있나': 'who holds this file',
    '감사 로그': 'audit log',
    'git pre-commit 훅 설치': 'install the git pre-commit hook',
    '{0}시간 {1}분': '{0}h {1}m',
    '{0}분': '{0}m',
    '잡혀 있는 파일이 없습니다.': 'Nothing is being held.',
    '  3. 정말 넘겨받아야 하면  agentlock claim <경로> -a {0} --force': '  3. If you truly must take it over:  agentlock claim <path> -a {0} --force',
    '아무도 안 잡고 있습니다.': 'Nobody is holding it.',
    'git 저장소가 아닙니다.': 'Not a git repository.',
    '덮어쓰려면 --force 를 쓰세요.': 'Use --force to overwrite it.',
    '잡을 수 없습니다. 다른 에이전트가 작업 중입니다.': 'Cannot claim it. Another agent is working on it.',
    '먼저 끝나기를 기다리거나, 다른 파일부터 작업하세요.': 'Wait for them to finish, or start on another file.',
    '정말 넘겨받아야 하면 --force 를 쓰되 감사 로그에 남습니다.': 'You can take it over with --force, but it goes in the audit log.',
    '풀 락이 없습니다.': 'There are no locks to release.',
    '작업 중인 에이전트 {0}': '{0} agents working',
    '  {0}  ({1}개 파일)': '  {0}  ({1} files)',
    '  {0}  ←  {1} ({2}, {3} 남음)': '  {0}  ←  {1} ({2}, {3} left)',
    '{0}  ({1}, {2} 남음)': '{0}  ({1}, {2} left)',
    '감사 로그가 아직 없습니다.': 'No audit log yet.',
    '설치했습니다: {0}': 'Installed: {0}',
    '준비됐습니다: {0}': 'Ready: {0}',
    'TTL 형식이 잘못됐습니다: {0}': 'Bad TTL format: {0}',
    '강제 회수: {0} ({1} → {2})': 'Taken by force: {0} ({1} → {2})',
    '거부  {0} 는 {1} 의 락입니다': 'DENIED  {0} is held by {1}',
    '해제  {0}': 'RELEASED  {0}',
    '{0}건이 만료되어 자동 해제됐습니다.': '{0} expired locks were released automatically.',
    '  ({0} 로부터)': '  (from {0})',
    '이미 pre-commit 훅이 있습니다: {0}': 'A pre-commit hook already exists: {0}',
    '경고: 락 파일이 깨져서 {0} 로 옮기고 새로 시작합니다.': 'Warning: the lock file was corrupt, so it was moved to {0} and started fresh.',
    '    └ {0} 가 {1} 를 잡고 있음 ({2} 시작, {3} 남음)': '    └ {0} is holding {1} (started {2}, {3} left)',
    '확보  {0}': 'CLAIMED  {0}',
    '연장  {0}': 'RENEWED  {0}',
    '({0}건이 만료되어 자동 해제됐습니다)': '({0} expired locks were released automatically)',
    '{0} 시작 / {1} 남음': 'started {0} / {1} left',
    '      메모: {0}': '      Note: {0}',
    '다른 프로세스가 {0} 를 잡고 있습니다. 계속 이러면 {1} 를 지우세요.': 'Another process is holding {0}. If it persists, delete {1}.',
}


if __name__ == "__main__":
    sys.exit(main())
