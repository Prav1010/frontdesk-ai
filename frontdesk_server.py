"""
FrontDesk AI backend (Python standard library only).

- /tools/*      -> HTTP tools that AssemblyAI calls for the voice agent
- /             -> live dashboard (bookings + tool-call activity)
- /api/state    -> JSON used by the dashboard

Run:  python frontdesk_server.py
Env:  PORT (default 8000), FRONTDESK_TOOL_KEY (shared secret), TZ_OFFSET_MINUTES (default 330 = India)
"""
import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8000"))
TOOL_KEY = os.environ.get("FRONTDESK_TOOL_KEY", "")
TZ = timezone(timedelta(minutes=int(os.environ.get("TZ_OFFSET_MINUTES", "330"))))
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookings.json")

BUSINESS = {
    "name": "Sunrise Dental Clinic",
    "address": "12 Ring Road, Surat",
    "phone": "0261 555 0142",
    "hours": "Monday to Friday 9 AM to 5 PM, Saturday 9 AM to 1 PM, closed Sunday",
    "parking": "Free parking is available behind the building.",
    "payment": "We accept cash, cards and UPI. Most major insurance plans are accepted.",
    "emergency": "For dental emergencies during opening hours, call us and we will fit you in the same day.",
}
SERVICES = {
    "cleaning": {"label": "Teeth cleaning", "minutes": 30, "price": "Rs 800"},
    "checkup": {"label": "Dental checkup", "minutes": 30, "price": "Rs 500"},
    "filling": {"label": "Cavity filling", "minutes": 45, "price": "Rs 1500"},
    "whitening": {"label": "Teeth whitening", "minutes": 60, "price": "Rs 4000"},
}
# weekday(): Mon=0 ... Sun=6 -> (open_minute, close_minute)
HOURS = {0: (540, 1020), 1: (540, 1020), 2: (540, 1020), 3: (540, 1020), 4: (540, 1020), 5: (540, 780)}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

lock = threading.Lock()
events = []  # recent tool calls, newest last


# ---------- helpers ----------
def now():
    return datetime.now(TZ)


def fmt_time(m):
    h, mi = divmod(m, 60)
    suffix = "AM" if h < 12 else "PM"
    return f"{(h % 12) or 12}:{mi:02d} {suffix}"


def pretty_date(d):
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B %Y')}"


def parse_date(text):
    s = (text or "").strip().lower()
    s = re.sub(r"^(next|this|on)\s+", "", s)
    today = now().date()
    if s in ("today", "tonight"):
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)
    if s in DAYS:
        delta = (DAYS.index(s) - today.weekday()) % 7
        return today + timedelta(days=delta or 7)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3])).date()
        except ValueError:
            return None
    # free text: "Wednesday 23 September 2026", "23rd september", "september 23"
    s2 = re.sub(r"\b(" + "|".join(DAYS) + r")\b,?", " ", s)
    s2 = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s2).replace(",", " ")
    month = next((MONTHS[w[:3]] for w in s2.split() if w[:3] in MONTHS and w.isalpha()), None)
    nums = [int(w) for w in s2.split() if w.isdigit()]
    day = next((n for n in nums if 1 <= n <= 31), None)
    year = next((n for n in nums if n >= 1000), today.year)
    if month and day:
        try:
            d = datetime(year, month, day).date()
        except ValueError:
            return None
        if d < today and not any(n >= 1000 for n in nums):
            d = datetime(year + 1, month, day).date()
        return d
    return None


def parse_time(text):
    s = (text or "").strip().lower().replace(".", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        return None
    h, mi, ap = int(m[1]), int(m[2] or 0), m[3]
    if h > 23 or mi > 59:
        return None
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if ap is None and 1 <= h <= 7:  # "3" in a clinic means 3 PM
        h += 12
    return h * 60 + mi


def find_service(text):
    s = (text or "").strip().lower()
    for key in SERVICES:
        if key in s or key[:5] in s:
            return key
    return None


def load():
    with lock:
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []


def save(bookings):
    with lock:
        with open(DATA_FILE, "w") as f:
            json.dump(bookings, f, indent=2)


def seed_if_needed():
    if os.path.exists(DATA_FILE):
        return
    base = now().date()
    seeds = []
    for offset, start, key in [(1, 600, "checkup"), (1, 690, "cleaning"), (2, 840, "filling")]:
        d = base + timedelta(days=offset)
        if d.weekday() == 6:
            d += timedelta(days=1)
        seeds.append({
            "code": "FD-" + uuid.uuid4().hex[:4].upper(), "name": "Existing patient", "phone": "0000000000",
            "service": key, "date": d.isoformat(), "start": start, "seed": True,
            "created": now().isoformat(timespec="seconds"),
        })
    save(seeds)


def free_slots(d, key):
    """Start times (minutes) that fit the service and don't overlap existing bookings."""
    hours = HOURS.get(d.weekday())
    if not hours:
        return []
    dur = SERVICES[key]["minutes"]
    booked = [(b["start"], b["start"] + SERVICES[b["service"]]["minutes"])
              for b in load() if b["date"] == d.isoformat()]
    out = []
    start = hours[0]
    while start + dur <= hours[1]:
        overlaps = any(start < be and start + dur > bs for bs, be in booked)
        in_past = d == now().date() and start <= now().hour * 60 + now().minute
        if not overlaps and not in_past:
            out.append(start)
        start += 30
    return out


def log_event(tool, args, result):
    brief = result.get("message") or result.get("summary") or json.dumps(result)[:160]
    safe_args = {k: (("•••" + str(v)[-4:]) if k == "phone" else v) for k, v in args.items()}
    events.append({"ts": now().strftime("%H:%M:%S"), "tool": tool, "args": safe_args, "result": str(brief)[:200]})
    del events[:-50]


# ---------- tools ----------
def check_availability(a):
    d = parse_date(a.get("date"))
    key = find_service(a.get("service"))
    if not d:
        return {"ok": False, "message": "I could not understand the date. Ask the caller for a day like 'tomorrow' or 'Friday'."}
    if d < now().date():
        return {"ok": False, "message": "That date is in the past. Ask for a future date."}
    if not key:
        return {"ok": False, "message": "Unknown service. We offer: " + ", ".join(SERVICES) + "."}
    if d.weekday() not in HOURS:
        return {"ok": True, "open": False, "date": pretty_date(d), "message": "The clinic is closed on Sundays. Offer Monday instead."}
    slots = free_slots(d, key)
    if not slots:
        return {"ok": True, "date": pretty_date(d), "available_times": [], "message": "Fully booked that day. Offer another day."}
    return {
        "ok": True, "date": pretty_date(d), "iso_date": d.isoformat(),
        "service": SERVICES[key]["label"], "duration_minutes": SERVICES[key]["minutes"],
        "available_times": [fmt_time(s) for s in slots[:6]],
        "message": f"{len(slots)} openings on {pretty_date(d)}. Offer at most three.",
    }


def book_appointment(a):
    d = parse_date(a.get("date"))
    key = find_service(a.get("service"))
    t = parse_time(a.get("time"))
    name = (a.get("name") or "").strip()
    phone = re.sub(r"\D", "", a.get("phone") or "")
    problems = []
    if not name or name.lower() in ("john doe", "jane doe", "unknown", "caller"):
        problems.append("the caller's real full name (ask them, do not guess)")
    if len(phone) < 10:
        problems.append("the caller's phone number with at least 10 digits (ask them, do not guess)")
    if not key:
        problems.append("a valid service (cleaning, checkup, filling or whitening)")
    if not d:
        problems.append("a valid date")
    if t is None:
        problems.append("a valid time like 10:30 AM")
    if problems:
        return {"confirmed": False, "message": "Cannot book yet. Still needed: " + "; ".join(problems) + ". Ask the caller for it."}
    if t not in free_slots(d, key):
        alts = [fmt_time(s) for s in free_slots(d, key)[:3]]
        return {"confirmed": False, "message": "That time is not available. Offer these instead: " + (", ".join(alts) or "another day") + "."}
    booking = {
        "code": "FD-" + uuid.uuid4().hex[:4].upper(), "name": name, "phone": phone, "service": key,
        "date": d.isoformat(), "start": t, "created": now().isoformat(timespec="seconds"),
    }
    save(load() + [booking])
    return {
        "confirmed": True, "confirmation_code": booking["code"],
        "summary": f"{SERVICES[key]['label']} for {name} on {pretty_date(d)} at {fmt_time(t)}.",
        "message": "Booked. Read back the day, time and confirmation code.",
    }


def get_business_info(a):
    topic = (a.get("topic") or "").lower()
    for key in SERVICES:
        if key in topic or key[:5] in topic:
            s = SERVICES[key]
            return {"ok": True, "message": f"{s['label']} takes {s['minutes']} minutes and costs {s['price']}."}
    if any(w in topic for w in ("price", "cost", "fee", "rate", "services")):
        return {"ok": True, "message": "; ".join(f"{s['label']} {s['price']} ({s['minutes']} min)" for s in SERVICES.values())}
    for k in ("hours", "open", "address", "location", "phone", "parking", "payment", "insurance", "emergency", "pay"):
        if k in topic:
            field = {"open": "hours", "location": "address", "insurance": "payment", "pay": "payment"}.get(k, k)
            return {"ok": True, "message": BUSINESS[field]}
    return {"ok": True, "message": f"{BUSINESS['name']}, {BUSINESS['address']}. Hours: {BUSINESS['hours']}. If you cannot answer, offer to take a message."}


TOOLS = {"check_availability": check_availability, "book_appointment": book_appointment, "get_business_info": get_business_info}


# ---------- dashboard ----------
DASHBOARD = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FrontDesk AI</title><style>
:root{--bg:#0f1220;--card:#181c30;--fg:#e8eaf6;--mut:#8b90b0;--acc:#5eead4;--line:#2a2f4d}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px}
h1{font-size:18px;margin:0}.dot{width:9px;height:9px;border-radius:50%;background:var(--acc);box-shadow:0 0 8px var(--acc)}
main{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:16px 24px}@media(max-width:800px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;overflow:auto;max-height:80vh}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin:0 0 12px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500}.code{color:var(--acc);font-family:monospace}
.ev{padding:10px;border:1px solid var(--line);border-radius:8px;margin-bottom:8px;font-size:13px}
.ev b{color:var(--acc)}.ev small{color:var(--mut)}.empty{color:var(--mut);font-size:14px}
</style></head><body>
<header><span class="dot"></span><h1>FrontDesk AI &middot; __NAME__</h1></header>
<main><section class="card"><h2>Appointments</h2><div id="b"></div></section>
<section class="card"><h2>Live agent activity</h2><div id="e"></div></section></main>
<script>
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function tick(){try{const r=await fetch('/api/state');const s=await r.json();
document.getElementById('b').innerHTML=s.bookings.length?'<table><tr><th>Code</th><th>Patient</th><th>Service</th><th>When</th></tr>'+
s.bookings.map(b=>`<tr><td class="code">${esc(b.code)}</td><td>${esc(b.name)}<br><small style="color:#8b90b0">${esc(b.phone)}</small></td><td>${esc(b.service)}</td><td>${esc(b.when)}</td></tr>`).join('')+'</table>':'<div class="empty">No appointments yet.</div>';
document.getElementById('e').innerHTML=s.events.length?s.events.slice().reverse().map(e=>`<div class="ev"><small>${esc(e.ts)}</small> <b>${esc(e.tool)}</b><br><small>${esc(JSON.stringify(e.args))}</small><br>${esc(e.result)}</div>`).join(''):'<div class="empty">Waiting for a call...</div>';
}catch(e){}}
tick();setInterval(tick,1500);
</script></body></html>"""


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _args(self):
        args = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if isinstance(body, dict):
                    args.update(body)
            except json.JSONDecodeError:
                pass
        return args

    def _route(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, DASHBOARD.replace("__NAME__", BUSINESS["name"]), "text/html")
        if path == "/health":
            return self._send(200, {"ok": True})
        if path == "/api/state":
            bookings = []
            for b in sorted(load(), key=lambda x: (x["date"], x["start"])):
                d = datetime.fromisoformat(b["date"]).date()
                phone = b["phone"]
                bookings.append({
                    "code": b["code"], "name": b["name"], "phone": "•••• " + phone[-4:],
                    "service": SERVICES[b["service"]]["label"], "when": f"{pretty_date(d)}, {fmt_time(b['start'])}",
                })
            return self._send(200, {"bookings": bookings, "events": events})
        if path.startswith("/tools/"):
            if TOOL_KEY and self.headers.get("X-Tool-Key") != TOOL_KEY:
                return self._send(401, {"error": "unauthorized"})
            name = path[len("/tools/"):]
            if name not in TOOLS:
                return self._send(404, {"error": "unknown tool"})
            args = self._args()
            result = TOOLS[name](args)
            log_event(name, args, result)
            return self._send(200, result)
        return self._send(404, {"error": "not found"})

    do_GET = do_POST = lambda self: self._route()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    seed_if_needed()
    print(f"FrontDesk backend on http://localhost:{PORT}  (dashboard at /)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
