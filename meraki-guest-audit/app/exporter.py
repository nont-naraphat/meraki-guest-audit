"""
exporter.py -- Export Excel (streaming, กัน OOM) + CSV, และ syslog forwarder
"""
import os
import io
import csv
import socket
import logging
from datetime import datetime, timezone

from openpyxl import Workbook

log = logging.getLogger("exporter")

HEADERS = [
    ("guest_name", "ชื่อผู้ใช้ (Guest)"),
    ("guest_email", "อีเมล Guest"),
    ("sponsor_email", "อีเมล Sponsor (ผู้อนุมัติ)"),
    ("client_mac", "MAC"),
    ("client_ip", "IP"),
    ("client_description", "Device"),
    ("ssid", "SSID"),
    ("auth_reason", "Auth"),
    ("total_requested_time", "Duration (sec)"),
    ("authorized_at", "Authorized At (UTC)"),
    ("expires_at", "Expires At (UTC)"),
    ("first_captured_at", "First Seen (UTC)"),
    ("last_captured_at", "Last Seen (UTC)"),
]


def to_xlsx_bytes(rows: list[dict]) -> bytes:
    """openpyxl write_only=True -> streaming, ไม่กิน RAM แบบ non-streaming"""
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Guest Sessions")
    ws.append([h[1] for h in HEADERS])
    for r in rows:
        ws.append([r.get(k) for k, _ in HEADERS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_csv_bytes(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([h[1] for h in HEADERS])
    for r in rows:
        w.writerow([r.get(k, "") for k, _ in HEADERS])
    return buf.getvalue().encode("utf-8-sig")  # BOM ให้ Excel เปิดภาษาไทยไม่เพี้ยน


# ---------------- syslog forwarder ----------------
SYSLOG_ENABLED = os.getenv("SYSLOG_ENABLED", "false").lower() == "true"
SYSLOG_HOST    = os.getenv("SYSLOG_HOST", "192.168.0.6")
SYSLOG_PORT    = int(os.getenv("SYSLOG_PORT", "514"))
SYSLOG_PROTO   = os.getenv("SYSLOG_PROTO", "udp").lower()
SYSLOG_TAG     = os.getenv("SYSLOG_TAG", "meraki-guest-audit")
_FACILITY_USER = 1
_SEVERITY_INFO = 6
_PRI = _FACILITY_USER * 8 + _SEVERITY_INFO  # 14


def send_syslog(rec: dict):
    if not SYSLOG_ENABLED:
        return
    ts = datetime.now(timezone.utc).strftime("%b %d %H:%M:%S")
    host = socket.gethostname()
    msg = (
        f"guest_login guest_name=\"{rec.get('guest_name','')}\" "
        f"guest_email={rec.get('guest_email','')} "
        f"sponsor_email={rec.get('sponsor_email','')} "
        f"mac={rec.get('client_mac','')} ip={rec.get('client_ip','')} "
        f"ssid=\"{rec.get('ssid','')}\" "
        f"duration_sec={rec.get('total_requested_time','')} "
        f"authorized_at={rec.get('authorized_at','')}"
    )
    line = f"<{_PRI}>{ts} {host} {SYSLOG_TAG}: {msg}"
    try:
        if SYSLOG_PROTO == "tcp":
            with socket.create_connection((SYSLOG_HOST, SYSLOG_PORT), timeout=5) as s:
                s.sendall((line + "\n").encode("utf-8"))
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(line.encode("utf-8"), (SYSLOG_HOST, SYSLOG_PORT))
            s.close()
    except Exception as e:
        log.warning("ส่ง syslog ล้มเหลว: %s", e)
