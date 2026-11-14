#!/bin/bash
# BMS 서버 보안 보강 (2026-11-14 진단 I-01·I-02 조치). root 로 한 번 실행합니다. 여러 번 실행해도 안전합니다.
#   1) bms-deploy: 실행 위치 고정(cd /), 파이썬 격리 실행(-I), 부품 목록은 승인된 것만 설치, 상태 확인은 curl, 반영 직전 DB 백업
#   2) bms-backup: 실행 위치 고정, 파이썬 격리 실행, 차량 점검 사진·설정 파일 포함
#   3) bms-approve-requirements: 부품 목록이 바뀌었을 때 root 가 직접 승인 (배포 계정은 실행 불가)
# 이 스크립트는 BMS 것만 고칩니다. cpo 의 파일·계정·서비스·sudo 규칙은 건드리지 않습니다.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "root 로 실행해 주세요 (sudo bash $0)"; exit 1; }
cd /
BASE=/srv/apps/bms
[ -d "$BASE/app" ] || { echo "BMS 가 설치되어 있지 않습니다: $BASE/app"; exit 1; }
STAMP="$(date +%Y%m%d-%H%M%S)"
install -d -m 700 /root/bms-harden-backup
for f in bms-deploy bms-backup; do
    [ -f "/usr/local/sbin/$f" ] && cp -p "/usr/local/sbin/$f" "/root/bms-harden-backup/$f.$STAMP"
done

echo "== 1) 배포 스크립트 =="
cat > /usr/local/sbin/bms-deploy <<'EOF'
#!/bin/bash
# BMS 새 버전 반영: 부품 목록 확인 → DB 백업 → 이전본 보관 → 교체 → 재시작 → 실패 시 되돌림
# root 로 도는 동안 배포 계정이 만든 파일이 실행되지 않도록: 실행 위치를 / 로 고정, 파이썬은 격리(-I) 실행,
# 부품(라이브러리)은 root 가 승인한 목록과 같을 때만 그대로 두고, 다르면 설치하지 않고 멈춥니다.
set -euo pipefail
cd /
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset PYTHONPATH PYTHONSTARTUP PYTHONHOME PIP_CONFIG_FILE PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_FIND_LINKS
BASE=/srv/apps/bms
SRC=/home/bmsadmin/deploy/bms-app.tgz
APPROVED=/etc/bms/requirements.sha256
STAMP="$(date +%Y%m%d-%H%M%S)"
PORT="$(awk -F' *= *' '$1 == "port" {print $2}' /etc/bms/bms.toml)"
case "$PORT" in ''|*[!0-9]*) echo "포트 설정을 읽지 못했습니다"; exit 1 ;; esac
[ -f "$SRC" ] || { echo "배포 파일이 없습니다: $SRC"; exit 1; }
[ -L "$SRC" ] && { echo "배포 파일이 링크입니다 (거부)"; exit 1; }

STAGE="$(mktemp -d "$BASE/.stage.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
tar -xzf "$SRC" -C "$STAGE" --no-same-owner --no-same-permissions
[ -f "$STAGE/main.py" ] || { echo "main.py 가 없는 배포 파일입니다"; exit 1; }
if find "$STAGE" -type l | grep -q .; then echo "배포 파일 안에 링크가 있습니다 (거부)"; exit 1; fi
chown -R root:bms "$STAGE"
chmod -R u=rwX,g=rX,o= "$STAGE"

# 부품 목록: 승인된 것과 같아야 함. 다르면 아무것도 설치하지 않고 멈춤 (root 가 bms-approve-requirements 로 승인)
NEW_SUM="$(sha256sum "$STAGE/requirements.txt" | cut -d' ' -f1)"
if [ ! -f "$APPROVED" ] || [ "$NEW_SUM" != "$(cat "$APPROVED")" ]; then
    echo "부품 목록(requirements.txt)이 승인된 것과 다릅니다."
    echo "서버 관리자가 터미널에서 다음을 실행해 내용을 확인·승인한 뒤 다시 반영하세요:"
    echo "    sudo /usr/local/sbin/bms-approve-requirements"
    exit 2
fi

healthy() {
    for _ in $(seq 1 30); do
        if curl -fs -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/login"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# 반영 직전 DB 백업 (새 버전이 DB 구조를 바꾼 뒤 문제가 생겨도 되돌릴 수 있게). 최근 5개 보관.
install -d -m 700 /srv/backup/bms
if [ -f "$BASE/data/bms.db" ]; then
    "$BASE/venv/bin/python" -I -c "
import sqlite3
src, dst = sqlite3.connect('$BASE/data/bms.db'), sqlite3.connect('/srv/backup/bms/predeploy-$STAMP.db')
src.backup(dst)
assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
"
    chmod 600 "/srv/backup/bms/predeploy-$STAMP.db"
    ls -1t /srv/backup/bms/predeploy-*.db 2>/dev/null | tail -n +6 | xargs -r rm -f
fi

systemctl stop bms || true
if [ -f "$BASE/app/main.py" ]; then
    mv "$BASE/app" "/srv/backup/bms/app-$STAMP"
else
    rmdir "$BASE/app"
fi
mv "$STAGE" "$BASE/app"
chmod 750 "$BASE/app"
trap - EXIT
systemctl enable --now bms
if healthy; then
    ls -1dt /srv/backup/bms/app-* 2>/dev/null | tail -n +6 | xargs -r rm -rf
    echo "반영 완료: $STAMP"
    exit 0
fi
echo "시작 실패, 이전 버전으로 되돌립니다"
journalctl -u bms -n 30 --no-pager
systemctl stop bms || true
if [ -d "/srv/backup/bms/app-$STAMP" ]; then
    mv "$BASE/app" "/srv/backup/bms/failed-$STAMP"
    mv "/srv/backup/bms/app-$STAMP" "$BASE/app"
    echo "프로그램을 이전 버전으로 되돌렸습니다. DB 는 그대로입니다 (반영 직전 사본: /srv/backup/bms/predeploy-$STAMP.db)"
    systemctl start bms
fi
exit 1
EOF
chown root:root /usr/local/sbin/bms-deploy
chmod 750 /usr/local/sbin/bms-deploy

echo "== 2) 부품 목록 승인 명령 (root 전용, 배포 계정은 실행 불가) =="
cat > /usr/local/sbin/bms-approve-requirements <<'EOF'
#!/bin/bash
# 올라온 배포 파일의 부품 목록을 보여 주고, 승인하면 설치한 뒤 승인 기록을 남깁니다. 반드시 사람이 내용을 보고 실행합니다.
set -euo pipefail
cd /
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset PYTHONPATH PYTHONSTARTUP PYTHONHOME PIP_CONFIG_FILE PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_FIND_LINKS
BASE=/srv/apps/bms
SRC=/home/bmsadmin/deploy/bms-app.tgz
[ -f "$SRC" ] && [ ! -L "$SRC" ] || { echo "배포 파일이 없습니다: $SRC"; exit 1; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar -xzf "$SRC" -C "$TMP" --no-same-owner --no-same-permissions requirements.txt 2>/dev/null \
    || tar -xzf "$SRC" -C "$TMP" --no-same-owner --no-same-permissions ./requirements.txt
REQ="$TMP/requirements.txt"
# 부품 이름·버전만 허용: 옵션(-로 시작), 주소(://), 파일 경로(/ \ @)가 있으면 거부
if grep -vE '^\s*(#.*)?$' "$REQ" | grep -qE '^\s*-|://|^\s*[./\\]|@'; then
    echo "부품 목록에 옵션·주소·경로가 들어 있습니다. 설치하지 않습니다:"; grep -nE '^\s*-|://|^\s*[./\\]|@' "$REQ"; exit 1
fi
echo "──── 설치할 부품 목록 ────"; grep -vE '^\s*(#.*)?$' "$REQ"; echo "──────────────────────"
if [ -f /srv/apps/bms/app/requirements.txt ]; then
    echo "지금 것과 다른 줄:"; diff <(grep -vE '^\s*(#.*)?$' /srv/apps/bms/app/requirements.txt) <(grep -vE '^\s*(#.*)?$' "$REQ") || true
fi
read -r -p "이 목록을 승인하고 설치할까요? (yes 입력): " ans
[ "$ans" = "yes" ] || { echo "취소했습니다"; exit 1; }
"$BASE/venv/bin/python" -I -m pip install -q --only-binary :all: --isolated -r "$REQ"
chown -R root:bms "$BASE/venv"
chmod -R u=rwX,g=rX,o= "$BASE/venv"
sha256sum "$REQ" | cut -d' ' -f1 > /etc/bms/requirements.sha256
chmod 644 /etc/bms/requirements.sha256
echo "승인·설치 완료. 이제 bms-deploy 로 반영할 수 있습니다."
EOF
chown root:root /usr/local/sbin/bms-approve-requirements
chmod 700 /usr/local/sbin/bms-approve-requirements
# 지금 돌고 있는 버전의 부품 목록을 승인된 것으로 기록 (이미 설치되어 동작 중인 목록)
if [ -f "$BASE/app/requirements.txt" ]; then
    sha256sum "$BASE/app/requirements.txt" | cut -d' ' -f1 > /etc/bms/requirements.sha256
    chmod 644 /etc/bms/requirements.sha256
fi

echo "== 3) 백업 스크립트 =="
if [ -f /usr/local/sbin/bms-backup ]; then
cat > /usr/local/sbin/bms-backup <<'EOF'
#!/bin/bash
# BMS DB·자산 사진·차량 점검 사진·설정 암호화 백업: 서버 7일, NAS 30일 보관
set -euo pipefail
cd /
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset PYTHONPATH PYTHONSTARTUP PYTHONHOME
umask 077
BASE=/srv/apps/bms
DEST=/srv/backup/bms
NAS="/mnt/bmsnas/#BMS백업"
KEY=/etc/bms/backup.key
OUT="$DEST/bms-$(date +%Y%m%d-%H%M).tgz.enc"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
"$BASE/venv/bin/python" -I -c "
import sqlite3
src, dst = sqlite3.connect('$BASE/data/bms.db'), sqlite3.connect('$TMP/bms.db')
src.backup(dst)
assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
"
install -d -o bms -g bms -m 700 "$BASE/data/asset_photos" "$BASE/data/vehicle_photos"
cp -p /etc/bms/bms.toml "$TMP/bms.toml"
tar -czf - -C "$TMP" bms.db bms.toml -C "$BASE/data" asset_photos vehicle_photos \
    | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass "file:$KEY" -out "$OUT"
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass "file:$KEY" -in "$OUT" | tar -tzf - > "$TMP/list"
grep -qx 'bms.db' "$TMP/list"
grep -q '^vehicle_photos/' "$TMP/list"
find "$DEST" -maxdepth 1 -name 'bms-*.tgz.enc' -mtime +7 -delete
cp --no-preserve=all "$OUT" "$NAS/"
find "$NAS" -maxdepth 1 -name 'bms-*.tgz.enc' -mtime +30 -delete
echo "백업 완료: $(basename "$OUT") ($(du -h "$OUT" | cut -f1)), NAS 복사 완료"
EOF
chown root:root /usr/local/sbin/bms-backup
chmod 750 /usr/local/sbin/bms-backup
else
    echo "bms-backup 이 아직 없습니다 (2단계 설치 전) — 건너뜀"
fi

echo "== 4) 확인 =="
bash -n /usr/local/sbin/bms-deploy
bash -n /usr/local/sbin/bms-approve-requirements
[ -f /usr/local/sbin/bms-backup ] && bash -n /usr/local/sbin/bms-backup
command -v curl >/dev/null || { echo "curl 이 없습니다: apt-get install -y curl 이 필요합니다"; exit 1; }
visudo -cf /etc/sudoers.d/bms
grep -c "bms-approve-requirements" /etc/sudoers.d/bms >/dev/null && { echo "sudo 규칙에 승인 명령이 들어 있으면 안 됩니다"; exit 1; } || true
echo "승인된 부품 목록 지문: $(cat /etc/bms/requirements.sha256 2>/dev/null || echo 없음)"
if [ -f /usr/local/sbin/bms-backup ]; then
    echo "백업을 한 번 실행합니다…"
    /usr/local/sbin/bms-backup
fi
echo "보안 보강 완료 ($STAMP). 이전 스크립트 사본: /root/bms-harden-backup/"
