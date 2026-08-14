#!/usr/bin/env bash
#
# agentlock 검증 스크립트
#
#   ./test_agentlock.sh
#
# /tmp 아래에 임시 git 저장소를 만들어 돌리고, 끝나면 지웁니다.
# 검사 항목: 락 확보/거부/해제, TTL 만료, 디렉터리 락, 글로브,
#            --force 감사 기록, pre-commit 훅 차단, 동시 20개 실행 시 락 유실.
#
# 환경변수
#   AGENTLOCK_PY   검사할 agentlock.py 경로 (기본: 이 스크립트와 같은 디렉터리)

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
AGENTLOCK_PY="${AGENTLOCK_PY:-$SCRIPT_DIR/agentlock.py}"

# ---------------------------------------------------------------- 출력
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_G=$'\033[32m'; C_R=$'\033[31m'; C_D=$'\033[2m'; C_B=$'\033[1m'; C_0=$'\033[0m'
else
    C_G=""; C_R=""; C_D=""; C_B=""; C_0=""
fi

PASS=0
FAIL=0

section() { printf '\n%s%s%s\n' "$C_B" "$1" "$C_0"; }
ok()      { PASS=$((PASS + 1)); printf '  %sPASS%s  %s\n' "$C_G" "$C_0" "$1"; }
bad()     {
    FAIL=$((FAIL + 1))
    printf '  %sFAIL%s  %s\n' "$C_R" "$C_0" "$1"
    if [ -n "${2:-}" ]; then printf '        %s%s%s\n' "$C_D" "$2" "$C_0"; fi
    return 0
}
# check <기대> <실제> <설명>
check()   { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3" "기대: $1 / 실제: $2"; fi; return 0; }

die() { printf '\n%s%s%s\n' "$C_R" "$1" "$C_0" >&2; exit 2; }

# ---------------------------------------------------------------- 사전 확인
[ -f "$AGENTLOCK_PY" ] || die "agentlock.py 를 찾을 수 없습니다: $AGENTLOCK_PY"

PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1 &&
       "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
        PY="$cand"; break
    fi
done
[ -n "$PY" ] || die "파이썬 3.8 이상이 필요합니다."
command -v git >/dev/null 2>&1 || die "git 이 필요합니다."

# ---------------------------------------------------------------- 임시 저장소
WORK="$(mktemp -d "/tmp/agentlock-test.XXXXXXXX")" || die "임시 디렉터리를 만들 수 없습니다."
case "$WORK" in
    /tmp/agentlock-test.*) : ;;
    *) die "임시 경로가 예상과 다릅니다: $WORK" ;;
esac

cleanup() {
    local code=$?
    [ -d "$WORK" ] && rm -rf "$WORK"
    return "$code"
}
trap cleanup EXIT INT TERM

AL() { "$PY" "$AGENTLOCK_PY" "$@"; }
# 출력은 버리고 종료 코드만 보고 싶을 때
ALq() { "$PY" "$AGENTLOCK_PY" "$@" >/dev/null 2>&1; }

fresh_repo() {
    rm -rf "$WORK/repo"
    mkdir -p "$WORK/repo/src/api"
    cd "$WORK/repo" || die "cd 실패"
    git init -q .
    git config user.email "test@example.com"
    git config user.name "tester"
    git config commit.gpgsign false
    printf 'a\n' > src/alpha.ts
    printf 'b\n' > src/beta.ts
    printf 'r\n' > src/api/routes.ts
    git add -A
    git commit -q -m "init"
    ALq init
}

# locks.json 에 특정 경로가 있는지
has_lock() {
    "$PY" - "$WORK/repo/.agentlock/locks.json" "$1" <<'EOF'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    d = {}
sys.exit(0 if sys.argv[2] in d else 1)
EOF
}

lock_count() {
    "$PY" - "$WORK/repo/.agentlock/locks.json" <<'EOF'
import json, sys
try:
    print(len(json.load(open(sys.argv[1], encoding="utf-8"))))
except Exception:
    print(0)
EOF
}

# 감사 로그에서 조건에 맞는 줄 수. audit_count <action> [key=value ...]
audit_count() {
    "$PY" - "$WORK/repo/.agentlock/audit.jsonl" "$@" <<'EOF'
import json, sys
path, action, pairs = sys.argv[1], sys.argv[2], sys.argv[3:]
want = dict(p.split("=", 1) for p in pairs)
n = 0
try:
    fh = open(path, encoding="utf-8")
except FileNotFoundError:
    print(0); raise SystemExit
for line in fh:
    line = line.strip()
    if not line:
        continue
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        continue
    if rec.get("action") != action:
        continue
    if all(str(rec.get(k)) == v for k, v in want.items()):
        n += 1
print(n)
EOF
}

printf '%sagentlock 검증%s\n' "$C_B" "$C_0"
printf '  대상    %s\n' "$AGENTLOCK_PY"
printf '  버전    %s\n' "$(AL --version 2>&1)"
printf '  파이썬  %s\n' "$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
printf '  작업    %s\n' "$WORK"

# =================================================================== 1
section "1. 락 확보 / 거부 / 해제"
fresh_repo

AL claim src/alpha.ts -a agent-a -t 30m --note "리팩터링" >/dev/null 2>&1
check 0 $? "확보: 빈 상태에서 claim 성공"

if has_lock "src/alpha.ts"; then ok "확보: locks.json 에 기록됨"
else bad "확보: locks.json 에 기록됨" "src/alpha.ts 가 없음"; fi

check 1 "$(audit_count claim path=src/alpha.ts agent=agent-a)" "확보: 감사 로그에 claim 1건"

out="$(AL claim src/alpha.ts -a agent-b -t 10m 2>&1)"; rc=$?
check 1 "$rc" "거부: 남이 잡은 파일은 종료 코드 1"
if printf '%s' "$out" | grep -q "agent-a"; then ok "거부: 누가 잡고 있는지 출력에 나옴"
else bad "거부: 누가 잡고 있는지 출력에 나옴" "$out"; fi

ALq claim src/beta.ts -a agent-b -t 10m
check 0 $? "거부: 안 겹치는 파일은 잡힘"

ALq claim src/alpha.ts -a agent-a -t 45m
check 0 $? "연장: 같은 에이전트가 다시 잡으면 성공"
check 1 "$(audit_count renew path=src/alpha.ts agent=agent-a)" "연장: 감사 로그에 renew 1건"

ALq release src/alpha.ts -a agent-b
check 1 $? "해제: 남의 락은 못 푼다 (종료 코드 1)"
if has_lock "src/alpha.ts"; then ok "해제: 거부된 락은 그대로 남아 있음"
else bad "해제: 거부된 락은 그대로 남아 있음" "락이 사라짐"; fi

ALq release src/alpha.ts -a agent-a
check 0 $? "해제: 내 락은 풀린다"
if has_lock "src/alpha.ts"; then bad "해제: locks.json 에서 사라짐" "아직 남아 있음"
else ok "해제: locks.json 에서 사라짐"; fi

ALq claim src/alpha.ts -a agent-b -t 10m
check 0 $? "해제: 푼 다음에는 다른 에이전트가 잡을 수 있다"

ALq release -a agent-b --all
check 0 $? "해제: --all 로 내가 잡은 것 전부"
check 0 "$(lock_count)" "해제: --all 이후 남은 락 0건"

# =================================================================== 2
section "2. TTL 만료 자동 해제"
fresh_repo

ALq claim src/alpha.ts -a agent-a -t 2s
check 0 $? "TTL: 2초짜리 락 확보"
if has_lock "src/alpha.ts"; then ok "TTL: 만료 전에는 살아 있음"
else bad "TTL: 만료 전에는 살아 있음" "바로 사라짐"; fi

ALq claim src/alpha.ts -a agent-b -t 10m
check 1 $? "TTL: 만료 전에는 남이 못 잡음"

sleep 3

out="$(AL status 2>&1)"
if printf '%s' "$out" | grep -q "만료"; then ok "TTL: status 가 만료를 알린다"
else bad "TTL: status 가 만료를 알린다" "$out"; fi

check 0 "$(lock_count)" "TTL: 만료된 락이 locks.json 에서 걷힘"
check 1 "$(audit_count expire path=src/alpha.ts agent=agent-a)" "TTL: 감사 로그에 expire 1건"

ALq claim src/alpha.ts -a agent-b -t 10m
check 0 $? "TTL: 만료 후에는 다른 에이전트가 잡을 수 있다"

# =================================================================== 3
section "3. 디렉터리 락이 하위 파일을 덮는지"
fresh_repo

ALq claim src/api -a agent-a -t 10m
check 0 $? "디렉터리: src/api 확보"

ALq claim src/api/routes.ts -a agent-b -t 10m
check 1 $? "디렉터리: 하위 파일 src/api/routes.ts 가 막힌다"

out="$(AL who src/api/routes.ts 2>&1)"
if printf '%s' "$out" | grep -q "agent-a"; then ok "디렉터리: who 가 상위 디렉터리 락을 찾아준다"
else bad "디렉터리: who 가 상위 디렉터리 락을 찾아준다" "$out"; fi

ALq claim src/alpha.ts -a agent-b -t 10m
check 0 $? "디렉터리: 범위 밖 파일은 안 막힌다"

ALq release -a agent-a --all
ALq claim src/api/routes.ts -a agent-a -t 10m
check 0 $? "역방향: 하위 파일 먼저 확보"
ALq claim src/api -a agent-b -t 10m
check 1 $? "역방향: 하위가 잡혀 있으면 상위 디렉터리를 못 잡는다"

# =================================================================== 4
section "4. 글로브 패턴"
fresh_repo

ALq claim 'src/*.ts' -a agent-a -t 10m
check 0 $? "글로브: src/*.ts 확보"
if has_lock 'src/*.ts'; then ok "글로브: 패턴이 그대로 저장됨"
else bad "글로브: 패턴이 그대로 저장됨" "locks.json 키가 다름"; fi

ALq claim src/alpha.ts -a agent-b -t 10m
check 1 $? "글로브: 패턴에 걸리는 파일이 막힌다"

printf 'x\n' > README.md
ALq claim README.md -a agent-b -t 10m
check 0 $? "글로브: 패턴에 안 걸리는 파일은 잡힌다"

out="$(AL who src/beta.ts 2>&1)"
if printf '%s' "$out" | grep -q "agent-a"; then ok "글로브: who 가 패턴 락을 찾아준다"
else bad "글로브: who 가 패턴 락을 찾아준다" "$out"; fi

ALq release 'src/*.ts' -a agent-a
check 0 $? "글로브: 같은 패턴 문자열로 해제된다"
ALq claim src/alpha.ts -a agent-b -t 10m
check 0 $? "글로브: 해제 후에는 잡힌다"

# =================================================================== 5
section "5. --force 강제 회수가 감사 로그에 남는지"
fresh_repo

ALq claim src/alpha.ts -a agent-a -t 10m
ALq claim src/alpha.ts -a agent-b -t 10m
check 1 $? "force: --force 없이는 못 뺏는다"
check 0 "$(audit_count steal)" "force: 실패한 시도는 steal 로 남지 않는다"

out="$(AL claim src/alpha.ts -a agent-b -t 10m --force 2>&1)"; rc=$?
check 0 "$rc" "force: --force 로 회수 성공"
if printf '%s' "$out" | grep -q "강제 회수"; then ok "force: 회수 사실을 화면에 알린다"
else bad "force: 회수 사실을 화면에 알린다" "$out"; fi

check 1 "$(audit_count steal path=src/alpha.ts agent=agent-b taken_from=agent-a)" \
    "force: 감사 로그에 steal 1건 (agent-a → agent-b)"

owner="$("$PY" -c "import json;print(json.load(open('$WORK/repo/.agentlock/locks.json'))['src/alpha.ts']['agent'])" 2>/dev/null)"
check "agent-b" "$owner" "force: 소유자가 실제로 바뀐다"

# 디렉터리 락도 --force 로 뚫으면 steal 로 남아야 한다
ALq claim src/api -a agent-a -t 10m
ALq claim src/api/routes.ts -a agent-c -t 10m --force
check 1 "$(audit_count steal path=src/api agent=agent-c taken_from=agent-a)" \
    "force: 덮고 있던 디렉터리 락도 steal 로 기록된다"

if AL log 2>&1 | grep -q "강제회수"; then ok "force: log 출력에 강제회수가 보인다"
else bad "force: log 출력에 강제회수가 보인다" "$(AL log 2>&1)"; fi

# 감사 로그는 append-only 여야 한다
before="$(wc -l < "$WORK/repo/.agentlock/audit.jsonl")"
ALq release -a agent-c --all
after="$(wc -l < "$WORK/repo/.agentlock/audit.jsonl")"
if [ "$after" -gt "$before" ]; then ok "감사 로그: 줄이 줄지 않고 늘어난다 ($before → $after)"
else bad "감사 로그: 줄이 줄지 않고 늘어난다" "$before → $after"; fi

# =================================================================== 6
section "6. git pre-commit 훅이 실제로 커밋을 막는지"
fresh_repo

out="$(AL install-hook 2>&1)"; rc=$?
check 0 "$rc" "훅: install-hook 성공"
if [ -x .git/hooks/pre-commit ]; then ok "훅: pre-commit 파일이 실행 가능"
else bad "훅: pre-commit 파일이 실행 가능" "없거나 실행 권한 없음"; fi

ALq install-hook
check 1 $? "훅: 기존 훅이 있으면 덮어쓰지 않고 멈춘다"
ALq install-hook --force
check 0 $? "훅: --force 면 덮어쓴다"

ALq claim src/alpha.ts -a agent-a -t 10m
printf 'changed by agent-b\n' >> src/alpha.ts
git add src/alpha.ts

base="$(git rev-list --count HEAD)"
out="$(AGENT_NAME=agent-b git commit -m "should be blocked" 2>&1)"; rc=$?
if [ "$rc" -ne 0 ]; then ok "훅: 남의 락이 섞인 커밋이 실패한다 (코드 $rc)"
else bad "훅: 남의 락이 섞인 커밋이 실패한다" "커밋이 통과함"; fi

now="$(git rev-list --count HEAD)"
check "$base" "$now" "훅: 차단됐으므로 커밋이 실제로 안 만들어졌다"

if printf '%s' "$out" | grep -q "커밋을 멈췄습니다"; then ok "훅: 왜 막혔는지 설명이 나온다"
else bad "훅: 왜 막혔는지 설명이 나온다" "$out"; fi
if printf '%s' "$out" | grep -q "src/alpha.ts"; then ok "훅: 문제되는 파일 이름이 나온다"
else bad "훅: 문제되는 파일 이름이 나온다" "$out"; fi

AGENT_NAME=agent-a git commit -q -m "owner commits" >/dev/null 2>&1
check 0 $? "훅: 락 주인은 그대로 커밋할 수 있다"
check "$((base + 1))" "$(git rev-list --count HEAD)" "훅: 주인 커밋은 실제로 기록된다"

# 아무도 안 잡은 파일은 통과해야 한다
ALq release -a agent-a --all
printf 'free edit\n' >> src/beta.ts
git add src/beta.ts
AGENT_NAME=agent-b git commit -q -m "free file" >/dev/null 2>&1
check 0 $? "훅: 락이 없으면 아무나 커밋할 수 있다"

# 만료된 락은 커밋을 막지 않아야 한다
ALq claim src/beta.ts -a agent-a -t 2s
printf 'more\n' >> src/beta.ts
git add src/beta.ts
sleep 3
AGENT_NAME=agent-b git commit -q -m "after ttl" >/dev/null 2>&1
check 0 $? "훅: 만료된 락은 커밋을 막지 않는다"

# =================================================================== 7
section "7. 동시 20개 실행 시 락 유실 0건"
fresh_repo

N=20
for i in $(seq 1 $N); do
    ( "$PY" "$AGENTLOCK_PY" claim "src/c$i.ts" -a "agent$i" -t 10m --note "동시성 $i" >/dev/null 2>&1 ) &
done
wait

got="$(lock_count)"
check "$N" "$got" "동시성: 서로 다른 $N 개 경로가 전부 남아 있다 (유실 $((N - got))건)"

missing=""
for i in $(seq 1 $N); do
    has_lock "src/c$i.ts" || missing="$missing src/c$i.ts"
done
if [ -z "$missing" ]; then ok "동시성: 빠진 경로 없음"
else bad "동시성: 빠진 경로 없음" "빠짐:$missing"; fi

check "$N" "$(audit_count claim)" "동시성: 감사 로그에도 claim $N 건 전부 남았다"

if "$PY" -c "import json;json.load(open('$WORK/repo/.agentlock/locks.json'))" >/dev/null 2>&1; then
    ok "동시성: locks.json 이 깨지지 않았다"
else
    bad "동시성: locks.json 이 깨지지 않았다" "JSON 파싱 실패"
fi

if [ -e "$WORK/repo/.agentlock/.guard" ]; then
    bad "동시성: 가드 파일이 남지 않았다" "$WORK/repo/.agentlock/.guard 가 남아 있음"
else
    ok "동시성: 가드 파일이 남지 않았다"
fi

# 같은 경로를 20개가 동시에 노리면 정확히 하나만 이겨야 한다
fresh_repo
RES="$WORK/race"
mkdir -p "$RES"
for i in $(seq 1 $N); do
    (
        if "$PY" "$AGENTLOCK_PY" claim src/alpha.ts -a "racer$i" -t 10m >/dev/null 2>&1; then
            : > "$RES/win.$i"
        fi
    ) &
done
wait
wins="$(find "$RES" -name 'win.*' | wc -l | tr -d ' ')"
check 1 "$wins" "동시성: 같은 경로를 노린 $N 개 중 정확히 1개만 성공"
check 1 "$(lock_count)" "동시성: 경합 후에도 락은 1건만 존재"

# =================================================================== 결과
cd / || true
printf '\n%s────────────────────────────────%s\n' "$C_D" "$C_0"
if [ "$FAIL" -eq 0 ]; then
    printf '%s통과 %d건, 실패 0건. 전부 통과했습니다.%s\n' "$C_G" "$PASS" "$C_0"
    exit 0
else
    printf '%s통과 %d건, 실패 %d건.%s\n' "$C_R" "$PASS" "$FAIL" "$C_0"
    exit 1
fi
