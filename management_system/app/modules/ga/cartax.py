"""카택스(CARTAX) REST API 연동: 차량·운행기록을 읽어 법인차량 운행일지로 가져옵니다.

  문서: https://public.carbeast.co.kr/   (플러스·프리미엄 등급에서 제공, 조회 전용)
  GET /cars, POST /drivings (검색기간 최대 100일), 인증 헤더 x-cartax-api-key

API 키는 설정 화면에서 입력하며 DB 에 암호화 저장합니다.
같은 운행기록은 external_id 로 구분해 중복 등록하지 않고, 카택스에서 수정된 건만 갱신합니다.
"""
import json
import logging
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from app.common import today
from app.db import audit, get_settings, query, query_one, set_setting
from app.modules.ga import repo

log = logging.getLogger("bms")

BASE_URL = "https://public.carbeast.co.kr"
MAX_RANGE_DAYS = 100
SYNC_HOURS = 6                     # 자동 동기화 주기
AUTO_LOOKBACK_DAYS = 35            # 자동 동기화 범위 (월말 수정분 반영)

_stop = threading.Event()
_thread: threading.Thread | None = None
_lock = threading.Lock()


class CartaxError(Exception):
    pass


def _api_key() -> str:
    return get_settings().get("cartax_api_key") or ""


def enabled() -> bool:
    return bool(_api_key())


def _call(path: str, key: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data, method="POST" if body is not None else "GET",
                                 headers={"x-cartax-api-key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        message = {401: "API 키가 올바르지 않습니다",
                   403: "카택스 등급 제한입니다 (API 는 플러스·프리미엄 등급에서 제공)",
                   422: "요청 형식 오류입니다", 400: "요청 값이 올바르지 않습니다"}.get(ex.code, f"카택스 오류 ({ex.code})")
        raise CartaxError(message) from ex
    except (urllib.error.URLError, TimeoutError, ValueError) as ex:
        raise CartaxError(f"카택스에 연결할 수 없습니다 ({ex.__class__.__name__})") from ex


def test_connection() -> str:
    key = _api_key()
    if not key:
        raise CartaxError("API 키가 입력되지 않았습니다")
    cars = _call("/cars", key)
    return f"연결 성공: 카택스 등록 차량 {len(cars)}대"


def _plate(value: str | None) -> str:
    return re.sub(r"\s", "", value or "")


def _windows(start: str, end: str) -> list[tuple[str, str]]:
    """검색기간 100일 제한에 맞춰 기간을 나눕니다."""
    a, b = datetime.fromisoformat(start).date(), datetime.fromisoformat(end).date()
    out = []
    while a <= b:
        last = min(a + timedelta(days=MAX_RANGE_DAYS - 1), b)
        out.append((a.isoformat(), last.isoformat()))
        a = last + timedelta(days=1)
    return out


def purpose_of(value: str | None) -> str:
    """카택스 운행 목적 → 운행기록부 구분."""
    text = re.sub(r"[\s·\-]", "", value or "")
    if "비업무" in text:
        return "비업무"
    if "출퇴근" in text or "출근" in text or "퇴근" in text:
        return "출퇴근"
    return "일반업무"


def _ensure_vehicles(cars: list[dict]) -> tuple[dict, int]:
    plates = {_plate(v["plate_no"]): v["id"] for v in repo.list_vehicles()}
    added = 0
    for c in cars:
        plate = _plate(c.get("carNumber"))
        if plate and plate not in plates:
            plates[plate] = repo.save("ga_vehicles", None, {
                "plate_no": c["carNumber"], "model": c.get("modelName") or "미상", "fuel": c.get("fuel"),
                "ownership": "소유", "status": "운행", "memo": "카택스에서 자동 등록",
            }, quiet=True)
            added += 1
    return plates, added


def _driver_index() -> dict:
    """이름이 유일한 임직원만 연결 (동명이인은 비고에 이름을 남김)."""
    names = {}
    for e in repo.employees_brief(active_only=False):
        names.setdefault(e["name"], []).append(e["id"])
    return {n: ids[0] for n, ids in names.items() if len(ids) == 1}


def _log_values(row: dict, vehicle_id: int, drivers: dict) -> dict:
    name = row.get("userName") or ""
    driver = drivers.get(name)
    start = int(row.get("fromOdometer") or 0)
    end = int(row.get("toOdometer") or 0)
    if end < start:
        end = start + int(row.get("distanceTraveled") or 0)
    memo = " ".join(x for x in [row.get("memo"), None if driver else f"[카택스 운전자: {name}]" if name else None] if x)
    return {
        "vehicle_id": vehicle_id, "drive_date": row.get("fromDate") or (row.get("fromTime") or "")[:10],
        "driver_id": driver, "purpose": purpose_of(row.get("purpose")),
        "destination": row.get("toLocation") or row.get("toAddress"), "km_start": start, "km_end": end,
        "fuel_cost": int(row.get("fuelCost") or 0), "toll_cost": int(row.get("tollgateCost") or 0) + int(row.get("etcCost") or 0),
        "note": memo, "external_id": f"cartax:{row.get('id')}", "external_updated": row.get("updateTime") or "",
    }


def sync(start: str, end: str) -> str:
    """기간 내 운행기록을 가져옵니다. 결과 요약 문구를 반환."""
    key = _api_key()
    if not key:
        raise CartaxError("API 키가 입력되지 않았습니다 (회사정보·코드 설정)")
    if not _lock.acquire(blocking=False):
        raise CartaxError("이미 동기화 중입니다")
    try:
        plates, car_added = _ensure_vehicles(_call("/cars", key))
        drivers = _driver_index()
        added = updated = skipped = 0
        for a, b in _windows(start, end):
            for row in _call("/drivings", key, {"startDate": a, "endDate": b}):
                vehicle_id = plates.get(_plate(row.get("carNumber")))
                values = _log_values(row, vehicle_id, drivers) if vehicle_id else None
                if not values or not values["drive_date"]:
                    skipped += 1
                    continue
                old = query_one("SELECT id, external_updated FROM ga_vehicle_logs WHERE external_id=?", (values["external_id"],))
                if not old:
                    repo.save("ga_vehicle_logs", None, values, quiet=True)
                    added += 1
                elif old["external_updated"] != values["external_updated"]:
                    repo.save("ga_vehicle_logs", old["id"], values, quiet=True)
                    updated += 1
        result = f"카택스 동기화: 운행기록 신규 {added}건, 수정 {updated}건" + (f", 차량 {car_added}대 등록" if car_added else "") + \
                 (f", 제외 {skipped}건" if skipped else "")
        set_setting("cartax_last_sync", f"{datetime.now():%Y-%m-%d %H:%M} · {result}")
        audit("ga", "카택스 동기화", f"{start}~{end}", result)
        return result
    finally:
        _lock.release()


def imported_count() -> int:
    return query("SELECT COUNT(*) c FROM ga_vehicle_logs WHERE external_id IS NOT NULL")[0]["c"]


def _loop():
    while not _stop.wait(60):
        try:
            if enabled():
                sync((today() - timedelta(days=AUTO_LOOKBACK_DAYS)).isoformat(), today().isoformat())
        except CartaxError as ex:
            log.warning("카택스 자동 동기화 실패: %s", ex)
            set_setting("cartax_last_sync", f"{datetime.now():%Y-%m-%d %H:%M} · 실패: {ex}")
        except Exception:  # 일시 오류로 스레드가 멈추지 않게 함
            log.exception("카택스 자동 동기화 오류")
        _stop.wait(SYNC_HOURS * 3600)


def start_worker():
    global _thread
    if _thread:
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="bms-cartax", daemon=True)
    _thread.start()


def stop_worker():
    global _thread
    _stop.set()
    _thread = None
