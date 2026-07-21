"""
meraki.py
- Official Dashboard API v1  : ใช้ API key ดึงรายชื่อ client บน guest SSID (stable)
- Internal dashboard endpoint: ใช้ session cookie ดึง wireless_bigacl
  (guest_name / guest_email / sponsor_email) -- unofficial, cookie หมดอายุได้
"""
import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("meraki")

# ---- Official API config ----
API_KEY      = os.getenv("MERAKI_API_KEY", "").strip()
API_BASE     = os.getenv("MERAKI_API_BASE", "https://api.meraki.com/api/v1").rstrip("/")
NETWORK_ID   = os.getenv("MERAKI_NETWORK_ID", "").strip()
GUEST_SSID   = os.getenv("GUEST_SSID_NAME", "").strip()          # กรองเฉพาะ SSID นี้ (ว่าง = ทุก SSID)
TIMESPAN     = int(os.getenv("CLIENT_TIMESPAN", "86400"))        # ดู client ย้อนหลังกี่วินาที (default 24h)

# ---- Internal dashboard scrape config ----
# ก๊อป URL เต็มจาก dev tools ("copy as cURL") ให้ลงท้ายด้วย /client_show/
#   เช่น https://n123.meraki.com/YourOrg/n/aB3xYz/manage/usage/client_show/
CLIENT_SHOW_URL = os.getenv("MERAKI_CLIENT_SHOW_URL", "").strip()
DASH_COOKIE     = os.getenv("MERAKI_DASH_COOKIE", "").strip()    # ค่า header Cookie ทั้งก้อนจาก browser
try:
    DASH_EXTRA_HEADERS = json.loads(os.getenv("MERAKI_DASH_HEADERS_JSON", "{}"))
except Exception:
    DASH_EXTRA_HEADERS = {}

_SCRAPE_CONCURRENCY = int(os.getenv("SCRAPE_CONCURRENCY", "4"))


def _api_headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


async def list_guest_clients() -> list[dict]:
    """official API: list clients ที่เคย active ในช่วง TIMESPAN, กรองเฉพาะ guest SSID"""
    if not (API_KEY and NETWORK_ID):
        raise RuntimeError("ยังไม่ได้ตั้ง MERAKI_API_KEY / MERAKI_NETWORK_ID")

    url = f"{API_BASE}/networks/{NETWORK_ID}/clients"
    params = {"timespan": TIMESPAN, "perPage": 1000}
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=30, headers=_api_headers()) as c:
        while url:
            r = await c.get(url, params=params)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "2"))
                log.warning("official API 429 -> รอ %ss", wait)
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            batch = r.json()
            out.extend(batch)
            # pagination ผ่าน Link header
            nxt = None
            link = r.headers.get("Link", "")
            for part in link.split(","):
                if 'rel=next' in part or 'rel="next"' in part:
                    m = re.search(r"<([^>]+)>", part)
                    if m:
                        nxt = m.group(1)
            url, params = (nxt, None) if nxt else (None, None)

    if GUEST_SSID:
        out = [x for x in out if (x.get("ssid") or "") == GUEST_SSID]
    return out


def _parse_relative(label: str):
    """แปลง 'in 3 days' / '11 days' / '45 minutes ago' -> timedelta (best-effort)"""
    if not label:
        return None
    s = label.strip().lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(week|day|hour|hr|minute|min|second|sec)", s)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2)
    mult = {
        "week": 604800, "day": 86400, "hour": 3600, "hr": 3600,
        "minute": 60, "min": 60, "second": 1, "sec": 1,
    }[unit]
    return timedelta(seconds=n * mult)


async def _scrape_one(client: httpx.AsyncClient, cid: str):
    """ยิง internal endpoint 1 client -> คืน wireless_bigacl entry ที่เป็น sponsored/splash (หรือ None)"""
    try:
        r = await client.get(CLIENT_SHOW_URL + cid)
        if r.status_code in (401, 403):
            raise PermissionError("cookie ของ dashboard หมดอายุ/ไม่ถูกต้อง (401/403)")
        r.raise_for_status()
        data = r.json()
    except PermissionError:
        raise
    except Exception as e:
        log.warning("scrape client %s ล้มเหลว: %s", cid, e)
        return None

    acl = data.get("wireless_bigacl") or []
    for entry in acl:
        # เก็บเฉพาะรายการที่มี identity ของ splash/sponsored
        if entry.get("guest_email") or entry.get("sponsor_email") or entry.get("guest_name"):
            return entry
    return None


async def get_splash_status(api_client: httpx.AsyncClient, cid: str) -> dict | None:
    """official API: splashAuthorizationStatus -> คำตอบชี้ขาดว่า authen ผ่านไหม
    คืน {is_authorized, authorized_at, expires_at} หรือ None ถ้าดึงไม่ได้"""
    url = f"{API_BASE}/networks/{NETWORK_ID}/clients/{cid}/splashAuthorizationStatus"
    try:
        r = await api_client.get(url)
        if r.status_code == 429:
            await asyncio.sleep(int(r.headers.get("Retry-After", "2")))
            r = await api_client.get(url)
        r.raise_for_status()
        ssids = (r.json() or {}).get("ssids", {}) or {}
    except Exception as e:
        log.warning("splashAuthorizationStatus %s ล้มเหลว: %s", cid, e)
        return None

    chosen = None
    for entry in ssids.values():
        if entry.get("isAuthorized"):        # เจอตัวที่ authorized -> เอาตัวนี้เลย
            chosen = entry
            break
        chosen = chosen or entry             # ไม่งั้นเก็บตัวแรกไว้เผื่อ
    if chosen is None:
        return None
    return {
        "is_authorized": bool(chosen.get("isAuthorized")),
        "authorized_at": chosen.get("authorizedAt"),
        "expires_at":    chosen.get("expiresAt"),
    }


async def enrich_with_splash(clients: list[dict]) -> list[dict]:
    """
    รับ list client จาก official API -> merge ข้อมูล wireless_bigacl เข้าไป
    คืนเฉพาะ client ที่มี splash/sponsored identity
    """
    if not (CLIENT_SHOW_URL and DASH_COOKIE):
        raise RuntimeError("ยังไม่ได้ตั้ง MERAKI_CLIENT_SHOW_URL / MERAKI_DASH_COOKIE")

    headers = {
        "Cookie": DASH_COOKIE,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "User-Agent": "Mozilla/5.0 (guest-audit-poller)",
    }
    headers.update(DASH_EXTRA_HEADERS)

    sem = asyncio.Semaphore(_SCRAPE_CONCURRENCY)
    results: list[dict] = []

    api_client = httpx.AsyncClient(timeout=30, headers=_api_headers())
    async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=False) as c, api_client:
        async def worker(cli: dict):
            cid = cli.get("id")
            if not cid:
                return
            async with sem:
                entry = await _scrape_one(c, cid)
            if not entry:
                return
            # timestamp โดยประมาณจาก label (fallback)
            now = datetime.now(timezone.utc)
            auth_td = _parse_relative(entry.get("authorized", ""))
            exp_td = _parse_relative(entry.get("expires", ""))
            authorized_at = (now - auth_td).isoformat() if auth_td else None
            expires_at = (now + exp_td).isoformat() if exp_td else None

            # ค่าชี้ขาดจาก official API (แม่นกว่า + ไม่ drift)
            is_authorized = None
            async with sem:
                st = await get_splash_status(api_client, cid)
            if st:
                is_authorized = st["is_authorized"]
                authorized_at = st["authorized_at"] or authorized_at   # ใช้ของ official ก่อน
                expires_at = st["expires_at"] or expires_at

            results.append({
                "guest_name":    entry.get("guest_name"),
                "guest_email":   entry.get("guest_email"),
                "sponsor_email": entry.get("sponsor_email"),
                "auth_reason":   entry.get("auth_reason") or entry.get("raw_auth_reason"),
                "total_requested_time": entry.get("total_requested_time"),
                "authorized_label": entry.get("authorized"),
                "expires_label":    entry.get("expires"),
                "authorized_at": authorized_at,
                "expires_at":    expires_at,
                "is_authorized": is_authorized,
                "client_id":     cid,
                "client_mac":    cli.get("mac"),
                "client_ip":     cli.get("ip"),
                "client_description": cli.get("description"),
                "ssid":          cli.get("ssid") or entry.get("name"),
                "raw_json":      json.dumps(entry, ensure_ascii=False),
            })

        await asyncio.gather(*(worker(x) for x in clients))
    return results
