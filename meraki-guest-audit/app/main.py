"""
main.py -- FastAPI app
- APScheduler poll ทุก POLL_INTERVAL_MIN นาที
- REST API: /api/sessions, /api/sync, /api/export/xlsx, /api/export/csv, /healthz
- Serve UI ที่ /
"""
import os
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Query
from fastapi.responses import Response, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import meraki, db, exporter

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

POLL_INTERVAL_MIN = int(os.getenv("POLL_INTERVAL_MIN", "5"))
RETENTION_DAYS    = int(os.getenv("RETENTION_DAYS", "120"))          # พ.ร.บ.คอมฯ ขั้นต่ำ 90 -> เผื่อ 120
AUTO_PURGE        = os.getenv("AUTO_PURGE_ENABLED", "false").lower() == "true"
CLEANUP_HOUR      = int(os.getenv("CLEANUP_HOUR", "3"))              # ชั่วโมง (UTC) ที่ลบ log เก่า
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="Meraki Guest Audit")
scheduler = AsyncIOScheduler(timezone="UTC")

_last_run = {"at": None, "ok": None, "new": 0, "seen": 0, "error": None}


async def poll_once() -> dict:
    """1 รอบ: official list -> internal enrich -> upsert -> syslog"""
    log.info("poll เริ่ม...")
    clients = await meraki.list_guest_clients()
    log.info("official API เจอ %d clients บน guest SSID", len(clients))
    enriched = await meraki.enrich_with_splash(clients)
    log.info("มี splash/sponsored identity %d รายการ", len(enriched))
    new_rows = db.upsert_sessions(enriched)
    for r in new_rows:
        exporter.send_syslog(r)
    result = {
        "at": datetime.now(timezone.utc).isoformat(),
        "ok": True, "new": len(new_rows), "seen": len(enriched), "error": None,
    }
    _last_run.update(result)
    log.info("poll เสร็จ: ใหม่ %d / เห็นทั้งหมด %d", len(new_rows), len(enriched))
    return result


async def _scheduled_job():
    try:
        await poll_once()
    except Exception as e:
        _last_run.update({"at": datetime.now(timezone.utc).isoformat(),
                          "ok": False, "error": str(e)})
        log.error("poll ล้มเหลว: %s", e)


def _cleanup_job():
    if not AUTO_PURGE:
        return
    deleted = db.purge_older_than(RETENTION_DAYS)
    log.info("auto-purge: ลบ %d session ที่เก่ากว่า %d วัน", deleted, RETENTION_DAYS)


@app.on_event("startup")
async def _startup():
    db.init()
    scheduler.add_job(_scheduled_job, "interval", minutes=POLL_INTERVAL_MIN,
                      id="poller", next_run_time=datetime.now(timezone.utc))
    scheduler.add_job(_cleanup_job, "cron", hour=CLEANUP_HOUR, minute=0, id="cleanup")
    scheduler.start()
    log.info("scheduler start (poll ทุก %d นาที, auto-purge=%s @%d วัน)",
             POLL_INTERVAL_MIN, AUTO_PURGE, RETENTION_DAYS)


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "last_run": _last_run,
        "retention_days": RETENTION_DAYS,
        "auto_purge": AUTO_PURGE,
        **db.stats(),
    }


@app.post("/api/purge")
async def api_purge(days: int = Query(RETENTION_DAYS, ge=0)):
    """ลบ log เก่ากว่า <days> วันด้วยมือ (days=0 = ลบทั้งหมด)"""
    deleted = db.purge_older_than(days)
    log.info("manual purge: ลบ %d session (>%d วัน)", deleted, days)
    return {"deleted": deleted, "days": days, **db.stats()}


@app.get("/api/sessions")
async def api_sessions(q: str = "", date_from: str = "", date_to: str = "",
                       limit: int = Query(5000, le=50000)):
    return JSONResponse(db.query(q, date_from, date_to, limit))


@app.post("/api/sync")
async def api_sync():
    """กด sync เดี๋ยวนี้"""
    try:
        return await poll_once()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/export/xlsx")
async def export_xlsx(q: str = "", date_from: str = "", date_to: str = ""):
    rows = db.query(q, date_from, date_to, limit=50000)
    data = exporter.to_xlsx_bytes(rows)
    fname = f"guest-audit-{datetime.now().strftime('%Y%m%d-%H%M')}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/export/csv")
async def export_csv(q: str = "", date_from: str = "", date_to: str = ""):
    rows = db.query(q, date_from, date_to, limit=50000)
    data = exporter.to_csv_bytes(rows)
    fname = f"guest-audit-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
    return Response(content=data, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# UI (ต้อง mount ท้ายสุด)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
