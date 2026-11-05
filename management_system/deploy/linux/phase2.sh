#!/bin/bash
# BMS 실사용 전환 (관리자 권한으로 한 번만 실행)
#   sudo bash phase2.sh <NAS 경로가 적힌 파일> [NAS 계정]
#
# 샘플 자료를 지우고 회사 설정·양식을 넣으며, NAS 임직원 폴더 연결과 암호화 백업을 구성합니다.
# 다른 시스템의 파일(fstab 포함)·계정·서비스는 수정하지 않습니다.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "sudo bash 로 실행하세요"; exit 1; }
NAS_PATH="$(tr -d '\r\n' < "${1:?NAS 경로 파일을 지정하세요}")"
NAS_USER="${2:-bms-svc}"
BASE=/srv/apps/bms
MNT=/mnt/bmsnas
UNIT=mnt-bmsnas.mount
SEED="$BASE/app/company_seed.json"
MARK=/etc/bms/.production
PYTHON="$BASE/venv/bin/python"

[ -f "$SEED" ] || [ -f "$MARK" ] || { echo "회사 설정 파일이 없습니다. 배포를 먼저 해야 합니다"; exit 1; }
read -rsp "NAS 계정 ${NAS_USER} 비밀번호: " NAS_PW
echo

echo "== NAS 연결 =="
command -v mount.cifs >/dev/null || apt-get -o DPkg::Lock::Timeout=120 install -y cifs-utils
umask 077
printf 'username=%s\npassword=%s\n' "$NAS_USER" "$NAS_PW" > /etc/bms/nas.cred
unset NAS_PW
umask 022
chown root:root /etc/bms/nas.cred
chmod 600 /etc/bms/nas.cred
mkdir -p "$MNT"
cat > "/etc/systemd/system/$UNIT" <<EOF
[Unit]
Description=BMS 임직원 서류 NAS 폴더
After=network-online.target
Wants=network-online.target

[Mount]
What=${NAS_PATH}
Where=${MNT}
Type=cifs
Options=credentials=/etc/bms/nas.cred,vers=3.0,iocharset=utf8,uid=bms,gid=bms,file_mode=0600,dir_mode=0700,noserverino,_netdev,nofail

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
if ! systemctl restart "$UNIT"; then
    journalctl -u "$UNIT" -n 10 --no-pager || true
    systemctl stop "$UNIT" 2>/dev/null || true
    rm -f "/etc/systemd/system/$UNIT" /etc/bms/nas.cred
    systemctl daemon-reload
    echo "NAS 연결 실패: 자료는 바뀌지 않았습니다. 계정·비밀번호·공유폴더 권한을 확인하세요"
    exit 1
fi
systemctl enable "$UNIT" >/dev/null
sudo -u bms ls "$MNT" >/dev/null
sudo -u bms mkdir -p "$MNT/#BMS백업"
sudo -u bms test -w "$MNT/#BMS백업" || { echo "NAS 폴더에 쓸 수 없습니다. 읽기/쓰기 권한을 확인하세요"; exit 1; }
echo "NAS 연결 정상"

echo "== 서비스 설정 =="
install -d /etc/systemd/system/bms.service.d
cat > /etc/systemd/system/bms.service.d/20-nas.conf <<EOF
[Unit]
After=${UNIT}

[Service]
ReadWritePaths=-${MNT}
EOF
rm -f /etc/systemd/system/bms.service.d/10-sample.conf
sed -i "s|^employee_root = .*|employee_root = \"${MNT}\"|; s|^intake_interval_sec = .*|intake_interval_sec = 60|" /etc/bms/bms.toml
systemctl daemon-reload

echo "== 샘플 정리 =="
systemctl stop bms
if [ ! -f "$MARK" ]; then
    (cd "$BASE/app" && sudo -u bms env BMS_CONFIG=/etc/bms/bms.toml PYTHONUTF8=1 TZ=Asia/Seoul \
        "$PYTHON" manage.py prepare-production "$SEED" --delete-all-business-data)
    touch "$MARK"
else
    echo "이미 정리됨: 건너뜀"
fi

echo "== 백업 =="
[ -f /etc/bms/backup.key ] || (umask 077; openssl rand -base64 48 > /etc/bms/backup.key)
chown root:root /etc/bms/backup.key
chmod 600 /etc/bms/backup.key
cat > /usr/local/sbin/bms-backup <<'EOF'
#!/bin/bash
# BMS DB·자산 사진 암호화 백업: 서버 7일, NAS 30일 보관
set -euo pipefail
umask 077
BASE=/srv/apps/bms
DEST=/srv/backup/bms
NAS="/mnt/bmsnas/#BMS백업"
KEY=/etc/bms/backup.key
OUT="$DEST/bms-$(date +%Y%m%d-%H%M).tgz.enc"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
"$BASE/venv/bin/python" -c "
import sqlite3, sys
src, dst = sqlite3.connect('$BASE/data/bms.db'), sqlite3.connect('$TMP/bms.db')
src.backup(dst)
assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
"
install -d -o bms -g bms -m 700 "$BASE/data/asset_photos"
tar -czf - -C "$TMP" bms.db -C "$BASE/data" asset_photos \
    | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass "file:$KEY" -out "$OUT"
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass "file:$KEY" -in "$OUT" | tar -tzf - > "$TMP/list"
grep -qx 'bms.db' "$TMP/list"
find "$DEST" -maxdepth 1 -name 'bms-*.tgz.enc' -mtime +7 -delete
cp --no-preserve=all "$OUT" "$NAS/"
find "$NAS" -maxdepth 1 -name 'bms-*.tgz.enc' -mtime +30 -delete
echo "백업 완료: $(basename "$OUT") ($(du -h "$OUT" | cut -f1)), NAS 복사 완료"
EOF
cat > /etc/systemd/system/bms-backup.service <<'EOF'
[Unit]
Description=BMS 암호화 백업

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/bms-backup
EOF
cat > /etc/systemd/system/bms-backup.timer <<'EOF'
[Unit]
Description=BMS 매일 백업

[Timer]
OnCalendar=*-*-* 03:00:00
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /usr/local/sbin/bms-manage <<'EOF'
#!/bin/bash
# 허용된 관리 명령만 서비스 계정으로 실행
case "${1:-}" in
    unlock|list-users) ;;
    *) echo "사용법: bms-manage unlock <아이디> | list-users"; exit 1 ;;
esac
cd /srv/apps/bms/app
exec sudo -u bms env BMS_CONFIG=/etc/bms/bms.toml PYTHONUTF8=1 TZ=Asia/Seoul /srv/apps/bms/venv/bin/python manage.py "$@"
EOF
cat > /usr/local/sbin/bms-show-keys <<'EOF'
#!/bin/bash
# 서버를 잃어도 복구할 수 있도록 두 키를 Vault 등 서버 밖에 보관하기 위한 명령 (채팅에 붙이지 말 것)
echo "BMS 암호화 마스터 키:"; cat /srv/apps/bms/secrets/bms_master.key; echo
echo "BMS 백업 키:"; cat /etc/bms/backup.key
EOF
chmod 750 /usr/local/sbin/bms-backup /usr/local/sbin/bms-manage /usr/local/sbin/bms-show-keys
cat >> /etc/sudoers.d/bms <<'EOF'
bmsadmin ALL=(root) NOPASSWD: /usr/local/sbin/bms-backup, /usr/local/sbin/bms-manage *
EOF
visudo -cf /etc/sudoers.d/bms

systemctl daemon-reload
systemctl enable --now bms-backup.timer
systemctl start bms
for _ in $(seq 1 30); do
    "$PYTHON" -c "import urllib.request as u; u.urlopen('http://127.0.0.1:3102/login', timeout=2)" 2>/dev/null && break
    sleep 1
done
/usr/local/sbin/bms-backup
rm -f "$SEED"
echo
echo "완료: BMS $(systemctl is-active bms), NAS $(systemctl is-active "$UNIT"), 백업 타이머 $(systemctl is-active bms-backup.timer)"
