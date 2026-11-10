#!/bin/bash
# BMS 계약서 폴더(NAS) 연결 (관리자 권한으로 한 번 실행)
#   sudo bash contracts_nas.sh <NAS 경로가 적힌 파일>
#
# phase2.sh 에서 만든 BMS 전용 NAS 자격(/etc/bms/nas.cred, 계정 bms-svc)을 그대로 씁니다.
# BMS 전용 mount 유닛만 추가하고 읽기 전용으로 연결합니다. 다른 시스템의 파일(fstab 포함)·계정·서비스는 수정하지 않습니다.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "sudo bash 로 실행하세요"; exit 1; }
NAS_PATH="$(tr -d '\r\n' < "${1:?NAS 경로 파일을 지정하세요}")"
MNT=/mnt/bmsga
UNIT=mnt-bmsga.mount
CONF=/etc/bms/bms.toml

[ -f /etc/bms/nas.cred ] || { echo "BMS NAS 자격 파일이 없습니다 (phase2.sh 를 먼저 실행해야 합니다)"; exit 1; }
[ -f "$CONF" ] || { echo "$CONF 가 없습니다"; exit 1; }

echo "== 계약서 폴더 연결 =="
mkdir -p "$MNT"
cat > "/etc/systemd/system/$UNIT" <<EOF
[Unit]
Description=BMS 계약서 NAS 폴더 (읽기 전용)
After=network-online.target
Wants=network-online.target

[Mount]
What=${NAS_PATH}
Where=${MNT}
Type=cifs
Options=credentials=/etc/bms/nas.cred,vers=3.0,iocharset=utf8,uid=bms,gid=bms,file_mode=0400,dir_mode=0500,noserverino,_netdev,nofail,ro

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
if ! systemctl restart "$UNIT"; then
    journalctl -u "$UNIT" -n 10 --no-pager || true
    systemctl stop "$UNIT" 2>/dev/null || true
    rm -f "/etc/systemd/system/$UNIT"
    systemctl daemon-reload
    echo "계약서 폴더 연결 실패: 바뀐 것은 없습니다. NAS 폴더 경로와 bms-svc 권한을 확인하세요"
    exit 1
fi
systemctl enable "$UNIT" >/dev/null
sudo -u bms ls "$MNT" >/dev/null || { echo "BMS 계정으로 폴더를 읽을 수 없습니다"; exit 1; }
echo "계약서 폴더 연결 정상 (읽기 전용)"

echo "== 서비스 설정 =="
install -d /etc/systemd/system/bms.service.d
cat > /etc/systemd/system/bms.service.d/21-nas-contracts.conf <<EOF
[Unit]
After=${UNIT}
EOF
if grep -q '^contract_root *=' "$CONF"; then
    sed -i "s|^contract_root *=.*|contract_root = \"${MNT}\"|" "$CONF"
elif grep -q '^employee_root *=' "$CONF"; then
    sed -i "/^employee_root *=/a contract_root = \"${MNT}\"" "$CONF"
else
    printf '\n[paths]\ncontract_root = "%s"\n' "$MNT" >> "$CONF"
fi
systemctl daemon-reload
systemctl restart bms
sleep 3
echo "완료: BMS $(systemctl is-active bms), 계약서 폴더 $(systemctl is-active "$UNIT"), cpo $(systemctl is-active cpo)"
