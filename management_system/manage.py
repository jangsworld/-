"""BMS 관리 명령 (서버 콘솔에서 관리자만 실행)

  python manage.py create-user <아이디> <이름> <admin|hr|viewer>
  python manage.py reset-password <아이디>
  python manage.py reset-2fa <아이디>
  python manage.py unlock <아이디>
  python manage.py list-users
  python manage.py backup [저장폴더]        DB 온라인 백업 (암호화된 상태로 복사)
  python manage.py check                     보안 설정 점검
"""
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import settings  # noqa: E402
from app.db import init_db, query_one, execute  # noqa: E402
from app.security import auth  # noqa: E402
from app.security.crypto import key_path  # noqa: E402


def _user(name):
    u = query_one("SELECT * FROM users WHERE username=?", (name,))
    if not u:
        sys.exit(f"사용자 없음: {name}")
    return u


def backup(dest=None):
    dest = dest or settings.backup_dir or os.path.join(settings.output_root, "BMS_백업")
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, f"bms_{datetime.now():%Y%m%d_%H%M%S}.db")
    src = sqlite3.connect(settings.db_path)
    dst = sqlite3.connect(path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"백업 완료: {path}\n※ 마스터키({key_path()})는 이 백업과 다른 장소에 별도 보관하세요.")


def check():
    ok = lambda b: "✅" if b else "⚠️ "  # noqa: E731
    print(f"{ok(settings.https)} HTTPS: {'사용' if settings.https else '미사용 → 외부 접속 금지'}")
    print(f"{ok(settings.require_2fa)} 2단계 인증 필수: {settings.require_2fa}")
    print(f"{ok(settings.secure_cookie)} 쿠키 Secure: {settings.secure_cookie}")
    print(f"{ok('*' not in settings.allowed_hosts)} 허용 호스트: {settings.allowed_hosts}")
    print(f"{ok(os.path.exists(key_path()) or bool(os.environ.get('BMS_MASTER_KEY')))} 마스터키: {key_path()}")
    kp = os.path.normpath(key_path())
    print(f"{ok(not kp.startswith(os.path.normpath(settings.data_dir)))} 키와 DB 폴더 분리")
    print(f"{ok(bool(settings.nas_root))} NAS 경로: {settings.nas_root or '(미설정: data/outbox 사용)'}")
    if settings.nas_root:
        print(f"{ok(os.path.isdir(settings.nas_root))} NAS 접근 가능: {os.path.isdir(settings.nas_root)}")
    print(f"   사용자 수: {auth.count_users()}명 (권장 5명 이하)")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    init_db()
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "create-user":
        pw = auth.generate_temp_password()
        auth.create_user(args[0], args[1], args[2], pw)
        print(f"생성: {args[0]} / 임시 비밀번호: {pw}")
    elif cmd == "reset-password":
        u = _user(args[0])
        pw = auth.generate_temp_password()
        auth.set_password(u["id"], pw, must_change=True)
        print(f"임시 비밀번호: {pw}")
    elif cmd == "reset-2fa":
        u = _user(args[0])
        auth.reset_totp(u["id"])
        auth.revoke_sessions(u["id"])
        print("2단계 인증 초기화 완료 (다음 로그인 시 재등록)")
    elif cmd == "unlock":
        u = _user(args[0])
        execute("UPDATE users SET failed_count=0, locked_until=NULL, is_active=1 WHERE id=?", (u["id"],))
        print("잠금 해제 완료")
    elif cmd == "list-users":
        for u in auth.list_users():
            print(f"{u['username']:15} {u['display_name']:10} {auth.ROLES[u['role']]:8} 사용={u['is_active']} 2FA={u['totp_enabled']}")
    elif cmd == "backup":
        backup(args[0] if args else None)
    elif cmd == "check":
        check()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
