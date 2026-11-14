#!/bin/bash
# BMS 전용 서버 초기 구성 (우분투). 관리자 권한으로 한 번만 실행합니다.
#   sudo bash bootstrap.sh <bmsadmin 공개키 파일> [포트]
#
# 같은 서버의 다른 시스템과 완전히 분리합니다. 이 스크립트는 BMS 전용 계정·폴더·서비스만
# 만들고, 다른 시스템의 파일·계정·서비스·방화벽 규칙은 수정하지 않습니다.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "sudo bash 로 실행하세요"; exit 1; }
PUBKEY_FILE="${1:?bmsadmin 공개키 파일을 지정하세요}"
PORT="${2:-3102}"
[ -s "$PUBKEY_FILE" ] || { echo "공개키 파일이 없습니다: $PUBKEY_FILE"; exit 1; }
if ss -ltn | grep -q ":${PORT} "; then echo "포트 ${PORT} 는 이미 사용 중입니다"; exit 1; fi

BASE=/srv/apps/bms
SERVER_IP="$(ip -4 route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')"

echo "== 패키지 =="
apt-get -qq update
apt-get -o DPkg::Lock::Timeout=120 install -y python3-venv

echo "== 계정 =="
id bms >/dev/null 2>&1 || useradd --system --shell /usr/sbin/nologin --home-dir "$BASE/data" --no-create-home bms
id bmsadmin >/dev/null 2>&1 || useradd --create-home --shell /bin/bash bmsadmin
install -d -o bmsadmin -g bmsadmin -m 700 /home/bmsadmin/.ssh /home/bmsadmin/deploy
install -o bmsadmin -g bmsadmin -m 600 "$PUBKEY_FILE" /home/bmsadmin/.ssh/authorized_keys

echo "== 폴더 =="
mkdir -p /srv/apps /srv/backup
install -d -o root -g root -m 755 "$BASE"
install -d -o root -g bms -m 750 "$BASE/app" /etc/bms
install -d -o bms -g bms -m 700 "$BASE/data" "$BASE/logs" "$BASE/secrets"
install -d -o root -g root -m 700 /srv/backup/bms
[ -x "$BASE/venv/bin/python" ] || python3 -m venv "$BASE/venv"
chown -R root:bms "$BASE/venv"
chmod -R u=rwX,g=rX,o= "$BASE/venv"

echo "== 설정 =="
[ -f /etc/bms/bms.toml ] || cat > /etc/bms/bms.toml <<EOF
[server]
host = "0.0.0.0"
port = ${PORT}
allowed_hosts = ["${SERVER_IP}", "127.0.0.1", "localhost"]
behind_proxy = false

[paths]
data_dir = "${BASE}/data"
secrets_dir = "${BASE}/secrets"
log_dir = "${BASE}/logs"
backup_dir = "/srv/backup/bms"
employee_root = ""
intake_interval_sec = 0

[security]
require_2fa = true

[pdf]
pdf_browser_channel = ""
pdf_browser_path = "/usr/bin/google-chrome-stable"
EOF
chown root:bms /etc/bms/bms.toml
chmod 640 /etc/bms/bms.toml

echo "== 서비스 =="
cat > /etc/systemd/system/bms.service <<EOF
[Unit]
Description=BMS 경영관리업무시스템
After=network-online.target
Wants=network-online.target

[Service]
User=bms
Group=bms
WorkingDirectory=${BASE}/app
Environment=BMS_CONFIG=/etc/bms/bms.toml PYTHONUTF8=1 TZ=Asia/Seoul HOME=${BASE}/data
ExecStart=${BASE}/venv/bin/python main.py
Restart=on-failure
RestartSec=5
ProtectSystem=strict
ReadWritePaths=${BASE}/data ${BASE}/logs ${BASE}/secrets
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
NoNewPrivileges=yes
CapabilityBoundingSet=
UMask=0077
MemoryMax=1536M

[Install]
WantedBy=multi-user.target
EOF
# 1단계 시험: 샘플 자료로만 구동. 실자료 전환 때 이 파일을 지웁니다.
install -d /etc/systemd/system/bms.service.d
cat > /etc/systemd/system/bms.service.d/10-sample.conf <<EOF
[Service]
Environment=BMS_SEED_SAMPLE=true
EOF

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

cat > /usr/local/sbin/bms-selftest <<'EOF'
#!/bin/bash
# 서비스와 같은 격리 조건에서 PDF·OCR 을 점검하고 자원 사용량을 보여 줍니다.
BASE=/srv/apps/bms
systemd-run --quiet --pipe --wait --collect --uid=bms --gid=bms \
    -p WorkingDirectory="$BASE/app" \
    -p Environment="BMS_CONFIG=/etc/bms/bms.toml PYTHONUTF8=1 TZ=Asia/Seoul HOME=$BASE/data" \
    -p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes -p PrivateDevices=yes -p NoNewPrivileges=yes \
    -p ReadWritePaths="$BASE/data $BASE/logs $BASE/secrets" \
    "$BASE/venv/bin/python" "$BASE/app/deploy/linux/selftest.py"
echo "--- bms: $(systemctl is-active bms), 메모리 $(( $(systemctl show -p MemoryCurrent --value bms) / 1048576 ))MB"
free -m | awk 'NR==2 {print "--- 서버 메모리: 전체 " $2 "MB, 사용 가능 " $7 "MB"}'
EOF

cat > /usr/local/sbin/bms-initial-password <<'EOF'
#!/bin/bash
cat /srv/apps/bms/secrets/initial_admin_password.txt
EOF
chmod 750 /usr/local/sbin/bms-deploy /usr/local/sbin/bms-selftest /usr/local/sbin/bms-initial-password

echo "== 관리 권한 =="
cat > /etc/sudoers.d/bms <<'EOF'
bmsadmin ALL=(root) NOPASSWD: /usr/local/sbin/bms-deploy, /usr/local/sbin/bms-selftest, /usr/local/sbin/bms-initial-password
bmsadmin ALL=(root) NOPASSWD: /usr/bin/systemctl --no-pager status bms, /usr/bin/systemctl restart bms, /usr/bin/journalctl --no-pager -n 100 -u bms
EOF
chmod 440 /etc/sudoers.d/bms
visudo -cf /etc/sudoers.d/bms

echo "== SSH 허용 =="
# AllowUsers 는 누적되므로 기존 설정을 고치지 않고 bmsadmin 만 추가
echo "AllowUsers bmsadmin" > /etc/ssh/sshd_config.d/10-bms.conf
install -d -m 755 /run/sshd
if sshd -t; then
    systemctl try-reload-or-restart ssh
else
    rm -f /etc/ssh/sshd_config.d/10-bms.conf
    echo "SSH 설정 오류로 bmsadmin 허용을 취소했습니다"
    exit 1
fi

systemctl daemon-reload
systemctl enable bms
echo
echo "완료: 서버 ${SERVER_IP}, 포트 ${PORT}"
sshd -T | grep -i '^allowusers'
ufw status | grep -E "3101:3110|${PORT}" || echo "주의: 방화벽에 포트 ${PORT} 허용 규칙이 보이지 않습니다"

echo "※ 부품 목록 승인 명령(bms-approve-requirements)은 harden_20261114.sh 로 설치합니다. 첫 반영 전에 실행하세요."
