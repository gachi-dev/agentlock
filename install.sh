#!/bin/sh
# agentlock installer
#
#   curl -fsSL https://raw.githubusercontent.com/gachi-dev/agentlock/main/install.sh | sh
#
# 환경변수로 바꿀 수 있는 것:
#   AGENTLOCK_REPO  저장소 (기본 gachi-dev/agentlock). 포크했으면 여기만 바꾸세요.
#   AGENTLOCK_REF   브랜치/태그 (기본 main)
#   AGENTLOCK_URL   위 둘을 무시하고 직접 URL 지정
#   AGENTLOCK_BIN   설치할 디렉터리 (기본: 쓸 수 있으면 /usr/local/bin, 아니면 ~/.local/bin)
#
# 하는 일: 파이썬 3.8+ 확인 → 다운로드 → 내용 검증 → 실행권한 → PATH 안내.
# 하지 않는 일: sudo 실행, 셸 설정 파일 수정, 기존 파일 무단 삭제.

set -eu

REPO="${AGENTLOCK_REPO:-gachi-dev/agentlock}"
REF="${AGENTLOCK_REF:-main}"
URL="${AGENTLOCK_URL:-https://raw.githubusercontent.com/${REPO}/${REF}/agentlock.py}"

TMPDIR_="" # trap 에서 지울 임시 디렉터리

cleanup() {
    [ -n "$TMPDIR_" ] && [ -d "$TMPDIR_" ] && rm -rf "$TMPDIR_"
    return 0
}
trap cleanup EXIT INT TERM HUP

say()  { printf '%s\n' "$*"; }
info() { printf '  %s\n' "$*"; }

die() {
    printf '\n설치 실패: %s\n' "$1" >&2
    shift
    for line in "$@"; do
        printf '  %s\n' "$line" >&2
    done
    exit 1
}

# ---------------------------------------------------------------- 1. 파이썬
PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
            PY="$cand"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    die "파이썬 3.8 이상을 찾지 못했습니다." \
        "agentlock 은 파이썬 3.8+ 만 있으면 되고 다른 의존성은 없습니다." \
        "설치 후 다시 시도하세요:" \
        "  macOS   brew install python3" \
        "  Debian  sudo apt install python3" \
        "  RHEL    sudo dnf install python3"
fi

PYV="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
PYPATH="$(command -v "$PY")"
say "agentlock 설치"
info "파이썬  $PYV  ($PYPATH)"

# ---------------------------------------------------------------- 2. 설치 위치
if [ -n "${AGENTLOCK_BIN:-}" ]; then
    BIN="$AGENTLOCK_BIN"
elif [ -d /usr/local/bin ] && [ -w /usr/local/bin ]; then
    BIN=/usr/local/bin
else
    BIN="$HOME/.local/bin"
fi

if [ ! -d "$BIN" ]; then
    mkdir -p "$BIN" 2>/dev/null || die "설치 디렉터리를 만들 수 없습니다: $BIN" \
        "다른 위치를 쓰려면 AGENTLOCK_BIN 을 지정하세요." \
        "  AGENTLOCK_BIN=\$HOME/bin sh install.sh"
fi

if [ ! -w "$BIN" ]; then
    die "설치 디렉터리에 쓸 권한이 없습니다: $BIN" \
        "sudo 로 이 스크립트를 돌리는 대신, 쓸 수 있는 위치를 지정하세요." \
        "  AGENTLOCK_BIN=\$HOME/.local/bin sh install.sh"
fi

TARGET="$BIN/agentlock"
info "설치 위치  $TARGET"

# ---------------------------------------------------------------- 3. 다운로드
TMPDIR_="$(mktemp -d 2>/dev/null || mktemp -d -t agentlock)" \
    || die "임시 디렉터리를 만들 수 없습니다."
TMPFILE="$TMPDIR_/agentlock.py"

info "받는 중  $URL"
# https 로 시작하는 주소는 리다이렉트로 http 로 떨어지는 것까지 막는다.
# AGENTLOCK_URL 로 사내 미러(http, file 등)를 직접 지정한 경우는 그대로 둔다.
case "$URL" in
    https://*) SECURE=1 ;;
    *)         SECURE=0 ;;
esac

if command -v curl >/dev/null 2>&1; then
    if [ "$SECURE" -eq 1 ]; then
        curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 -o "$TMPFILE" "$URL" \
            || die "다운로드에 실패했습니다: $URL" \
                "주소가 맞는지, 네트워크가 되는지 확인하세요." \
                "포크를 쓴다면  AGENTLOCK_REPO=내계정/agentlock  을 지정하세요." \
                "브라우저로 파일을 받아 직접 설치해도 됩니다:" \
                "  chmod +x agentlock.py && mv agentlock.py $TARGET"
    else
        curl -fsSL -o "$TMPFILE" "$URL" \
            || die "다운로드에 실패했습니다: $URL" \
                "AGENTLOCK_URL 로 지정한 주소가 맞는지 확인하세요."
    fi
elif command -v wget >/dev/null 2>&1; then
    if [ "$SECURE" -eq 1 ]; then
        wget -q --https-only -O "$TMPFILE" "$URL" \
            || die "다운로드에 실패했습니다: $URL" \
                "주소가 맞는지, 네트워크가 되는지 확인하세요."
    else
        wget -q -O "$TMPFILE" "$URL" \
            || die "다운로드에 실패했습니다: $URL" \
                "AGENTLOCK_URL 로 지정한 주소가 맞는지 확인하세요."
    fi
else
    die "curl 도 wget 도 없습니다." \
        "둘 중 하나를 설치하거나, agentlock.py 를 직접 받아서 옮기세요:" \
        "  chmod +x agentlock.py && mv agentlock.py $TARGET"
fi

# ---------------------------------------------------------------- 4. 검증
# 404 HTML 이나 프록시 안내 페이지를 그대로 설치하면 나중에 이상하게 깨진다.
if [ ! -s "$TMPFILE" ]; then
    die "받은 파일이 비어 있습니다: $URL"
fi

if ! head -n 1 "$TMPFILE" | grep -q '^#!.*python'; then
    die "받은 파일이 agentlock 스크립트가 아닙니다." \
        "주소가 리다이렉트되거나 에러 페이지를 돌려줬을 수 있습니다:" \
        "  $URL"
fi

if ! grep -q 'agentlock - file ownership locks' "$TMPFILE"; then
    die "받은 파일 내용이 agentlock 과 맞지 않습니다." \
        "AGENTLOCK_URL 을 잘못 지정했는지 확인하세요:" \
        "  $URL"
fi

if ! "$PY" -c 'import ast,sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$TMPFILE"; then
    die "받은 파일이 올바른 파이썬 코드가 아닙니다. 전송 중 깨졌을 수 있습니다."
fi

# ---------------------------------------------------------------- 5. 설치
chmod 755 "$TMPFILE" || die "실행 권한을 줄 수 없습니다: $TMPFILE"

if [ -e "$TARGET" ] && [ ! -w "$TARGET" ]; then
    die "기존 파일을 덮어쓸 수 없습니다: $TARGET" \
        "권한을 확인하거나 AGENTLOCK_BIN 으로 다른 위치를 지정하세요."
fi

# mv 가 파일시스템을 넘나들면 실패할 수 있어 cp 로 폴백한다.
mv -f "$TMPFILE" "$TARGET" 2>/dev/null || {
    cp "$TMPFILE" "$TARGET" || die "설치에 실패했습니다: $TARGET"
    chmod 755 "$TARGET" || die "실행 권한을 줄 수 없습니다: $TARGET"
}

VERSION="$("$TARGET" --version 2>/dev/null || true)"
if [ -z "$VERSION" ]; then
    die "설치는 됐지만 실행이 안 됩니다: $TARGET" \
        "직접 한번 돌려보고 오류를 확인하세요:" \
        "  $TARGET --version"
fi

say ""
say "설치했습니다: $TARGET  ($VERSION)"

# ---------------------------------------------------------------- 6. PATH 안내
case ":${PATH}:" in
    *":${BIN}:"*)
        say ""
        say "바로 쓸 수 있습니다:"
        info "cd <저장소> && agentlock init"
        info "agentlock claim src/api.ts -a myagent -t 30m"
        ;;
    *)
        say ""
        say "$BIN 이 PATH 에 없습니다. 셸 설정에 아래 한 줄을 추가하세요."
        say ""
        info "export PATH=\"$BIN:\$PATH\""
        say ""
        say "추가할 파일은 셸에 따라 다릅니다:"
        info "bash  ~/.bashrc"
        info "zsh   ~/.zshrc"
        info "fish  ~/.config/fish/config.fish  (set -gx PATH $BIN \$PATH)"
        say ""
        say "지금 당장 쓰려면 전체 경로로 부르면 됩니다:"
        info "$TARGET --help"
        ;;
esac
