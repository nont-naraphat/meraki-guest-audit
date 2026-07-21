# Meraki Guest Audit

เก็บ log การ register ของ Sponsored Guest Login (ชื่อ / อีเมล guest / sponsor email / MAC / IP /
duration / เวลา) ที่ Meraki ไม่เก็บถาวรให้ — โดย **ไม่ต้องแตะ splash flow เดิม**

## ทำไมต้องมีตัวนี้
- Official Meraki API **ไม่มี** field guest_name / guest_email / sponsor_email
- data อยู่ใน internal endpoint `client_show` (`wireless_bigacl`) เท่านั้น และโผล่**เฉพาะตอน session ยัง active**
- ตัวนี้ poll ถี่ ๆ เพื่อ snapshot เก็บเข้า SQLite ถาวร -> สร้าง audit trail + export ได้

## ข้อจำกัดที่ต้องรู้ (สำคัญ)
- internal endpoint ใช้ **session cookie** ของ dashboard (ไม่ใช่ API key) → **cookie หมดอายุได้**
  พอ 401/403 ให้ก๊อป cookie ใหม่ใส่ `.env` แล้ว `docker compose restart`
- ตั้ง `POLL_INTERVAL_MIN` ให้ถี่พอ (5 นาที) กัน session สั้น ๆ หลุด

## Setup
1. `cp .env.example .env` แล้วเติมค่า:
   - `MERAKI_API_KEY`, `MERAKI_NETWORK_ID`, `GUEST_SSID_NAME`
   - `MERAKI_CLIENT_SHOW_URL` + `MERAKI_DASH_COOKIE`
     (DevTools > Network > request `client_show/<id>` > Copy as cURL → เอา URL ตัด id + Cookie)
2. `docker compose up -d --build`
3. เปิด `http://<synology>:8092`

## API
- `GET  /api/sessions?q=&date_from=&date_to=` — list
- `POST /api/sync` — poll เดี๋ยวนี้
- `GET  /api/export/xlsx` , `GET /api/export/csv`
- `GET  /healthz`
