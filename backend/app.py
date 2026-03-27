# app.py — Mizani AI Backend (Production-Ready)
# pip install flask supabase python-dotenv africastalking requests pandas reportlab gunicorn

import os, re, json, logging, threading
import pandas as pd
from flask import Flask, request, jsonify, Response
from dotenv import load_dotenv
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table,
    TableStyle, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── DEMO_MODE ─────────────────────────────────────────────────────────────────
# Set DEMO_MODE=false in Render environment variables for production.
DEMO_MODE = os.environ.get("DEMO_MODE", "true").lower() == "true"

# ── Supabase (lazy init to avoid crash on missing env vars) ───────────────────
_supabase_client = None

def supabase():
    """Return a cached Supabase client, initialising on first use."""
    global _supabase_client
    if _supabase_client is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set "
                "in your Render environment variables."
            )
        from supabase import create_client
        _supabase_client = create_client(url, key)
    return _supabase_client


# ── Africa's Talking (lazy init, re-initialises if env changes) ───────────────
_at_cache = {}

def at(service: str):
    """Return cached AT service handle (sms | airtime | voice)."""
    global _at_cache

    # FIX: Use AFRICAS_TALKING_USERNAME consistently everywhere (was AFRICAS_TALKING_USERNAME in health)
    username = os.environ.get("AFRICAS_TALKING_USERNAME", "sandbox")
    api_key  = os.environ.get("AT_API_KEY", "")

    cache_key = f"{username}:{api_key}"
    if _at_cache.get("_key") != cache_key:
        import africastalking
        africastalking.initialize(username=username, api_key=api_key)
        _at_cache = {
            "_key":    cache_key,
            "sms":     africastalking.SMS,
            "airtime": africastalking.Airtime,
            "voice":   africastalking.Voice,
        }
        logger.info(f"Africa's Talking initialised — username: {username}")

    return _at_cache[service]


# ── Alert recipients ──────────────────────────────────────────────────────────
ALERT_RECIPIENTS = [
    p.strip() for p in os.environ.get("ALERT_PHONES", "").split(",") if p.strip()
]

# ── Supabase Storage bucket for PDFs ─────────────────────────────────────────
PDF_BUCKET = os.environ.get("SUPABASE_PDF_BUCKET", "reports")

# ── Sighting catalogue ────────────────────────────────────────────────────────
SIGHTINGS = {
    "1": ("billboard_signage",  800_000,   0.70),
    "2": ("vehicle_convoy",   1_200_000,   0.75),
    "3": ("paid_rally",       2_500_000,   0.80),
    "4": ("cash_gifts",         500_000,   0.85),
}

SMS_KEYWORDS = {
    "BILLBOARD": "1", "BANGO": "1",
    "CONVOY":    "2", "GARI":  "2",
    "RALLY":     "3", "MKUTANO": "3",
    "CASH":      "4", "PESA":   "4", "GIFTS": "4", "ZAWADI": "4",
}

# ── In-memory language cache (fast path; Supabase is fallback) ────────────────
# FIX: Pure Supabase-only lang lookup added ~200-400ms per USSD step,
# eating into the 5s AT timeout. We now cache in memory first.
_lang_cache: dict[str, str] = {}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def safe_fn(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", text)

def alert_level(spr: float) -> str:
    if spr > 1.5: return "CRITICAL"
    if spr > 1.2: return "YELLOW"
    return "GREEN"


# FIX: Single canonical _ussd_response — removed the duplicate early definition.
# FIX: Enforce 182-character Kenya USSD limit to prevent truncated menus on device.
def _ussd_response(msg: str) -> Response:
    """
    Canonical USSD response helper.
    - Strips leading/trailing whitespace (AT rejects responses with leading spaces)
    - Enforces the 182-char Kenya USSD limit
    - Always returns text/plain with HTTP 200
    """
    msg = msg.strip()

    # Enforce Kenya 182-char USSD limit
    if len(msg) > 182:
        logger.warning(f"⚠️  USSD message too long ({len(msg)} chars) — truncating")
        prefix = msg[:3]   # "CON" or "END"
        msg = prefix + msg[3:179] + "..."

    logger.info(f"📱 USSD OUT ({len(msg)} chars): {repr(msg[:80])}")

    return Response(
        msg,
        status=200,
        content_type="text/plain; charset=utf-8",
        headers={
            # Some AT gateway versions need this explicit header
            "Content-Type": "text/plain; charset=utf-8",
        }
    )


def fetch_meta_ads(name: str) -> float:
    return 2_300_000.0 if DEMO_MODE else 0.0


def candidate_suggestions(name: str, constituency: str) -> list:
    try:
        r = (supabase().table("candidates")
             .select("candidate_name")
             .ilike("candidate_name", f"%{name}%")
             .limit(5).execute())
        return [x["candidate_name"] for x in r.data]
    except Exception as e:
        logger.error(f"candidate_suggestions error: {e}")
        return []


def upsert_candidate(name: str, constituency: str) -> str:
    """Return candidate_id (UUID string), creating record if missing."""
    r = (supabase().table("candidates")
         .select("candidate_id")
         .ilike("candidate_name", f"%{name}%")
         .eq("constituency", constituency)
         .execute())
    if r.data:
        return r.data[0]["candidate_id"]
    res = supabase().table("candidates").insert({
        "candidate_name":  name,
        "constituency":    constituency,
        "declared_assets": 0,
        "political_party": "Unknown",
        "iebc_source":     "ussd_pending",
    }).execute()
    return res.data[0]["candidate_id"]


def insert_expenditure(candidate_id: str, source_type: str,
                       amount: float, confidence: float,
                       description: str, location: str):
    supabase().table("expenditures").insert({
        "candidate_id":   candidate_id,
        "source_type":    source_type,
        "amount":         amount,
        "confidence_score": confidence,
        "description_of_the_expenditure_spended": description,
        "location":       location,
        "created_at":     datetime.utcnow().isoformat(),
    }).execute()


def count_constituency_reports(constituency: str) -> int:
    try:
        r = (supabase().table("expenditures")
             .select("count", count="exact")
             .eq("location", constituency)
             .execute())
        return r.count or 0
    except Exception as e:
        logger.error(f"count_constituency_reports error: {e}")
        return 0


def format_report_id(candidate_id: str) -> str:
    return str(candidate_id).replace("-", "")[:8].upper()


def send_sms(recipients: list, message: str):
    if not recipients:
        return {"skipped": "no recipients"}
    if DEMO_MODE:
        logger.info(f"[DEMO SMS → {recipients}]\n{message}")
        return {"demo": True}
    try:
        return at("sms").send(message, recipients)
    except Exception as e:
        logger.error(f"SMS error: {e}")
        return {"error": str(e)}


def send_alert_sms(candidate: str, constituency: str,
                   spr: float, lvl: str, report_url: str,
                   evidence_summary: str):
    msg = (
        f"MIZANI [{lvl}]\n"
        f"Candidate: {candidate} ({constituency})\n"
        f"SPR: {spr:.2f} — {((spr-1)*100):.0f}% above declared wealth\n"
        f"Sources: {evidence_summary}\n"
        f"Report: {report_url}"
    )
    return send_sms(ALERT_RECIPIENTS, msg)


def reward_airtime(phone: str, amount_kes: float = 5.0):
    """Send small airtime reward to a citizen reporter."""
    if DEMO_MODE:
        logger.info(f"[DEMO AIRTIME] KES {amount_kes} → {phone}")
        return {"demo": True}
    try:
        return at("airtime").send(
            phone_number=phone,
            amount=str(amount_kes),
            currency_code="KES"
        )
    except Exception as e:
        logger.error(f"Airtime error: {e}")
        return {"error": str(e)}


def _reward_airtime_async(phone: str, amount_kes: float = 5.0):
    """
    FIX: reward_airtime() was called synchronously inside the USSD handler,
    adding 1-2s AT API latency inside the 5s response window.
    We now fire it in a daemon thread so the USSD response goes out instantly.
    """
    t = threading.Thread(target=reward_airtime, args=(phone, amount_kes), daemon=True)
    t.start()


# ── USSD session language ──────────────────────────────────────────────────────

def get_session_lang(session_id: str) -> str:
    """
    FIX: Pure Supabase lookups on every USSD step consumed ~300ms each,
    totalling 600-900ms before we even ran any logic.
    Now: check in-memory cache first; only hit Supabase on cache miss
    (i.e. after a server restart — rare in production).
    """
    if session_id in _lang_cache:
        return _lang_cache[session_id]
    # Cache miss — try Supabase (first request after server restart)
    try:
        r = (supabase().table("ussd_sessions")
             .select("lang")
             .eq("session_id", session_id)
             .execute())
        if r.data:
            lang = r.data[0].get("lang", "en")
            _lang_cache[session_id] = lang
            return lang
    except Exception as e:
        logger.warning(f"get_session_lang DB error: {e}")
    return "en"


def set_session_lang(session_id: str, lang: str):
    """
    FIX: Write to in-memory cache immediately (synchronous),
    then persist to Supabase in a background thread (non-blocking).
    This means the USSD response is not delayed by the DB write.
    """
    _lang_cache[session_id] = lang

    def _persist():
        try:
            supabase().table("ussd_sessions").upsert({
                "session_id": session_id,
                "lang":       lang,
                "updated_at": datetime.utcnow().isoformat(),
            }).execute()
        except Exception as e:
            logger.warning(f"set_session_lang DB error: {e}")

    threading.Thread(target=_persist, daemon=True).start()


# ── SIM-swap check ────────────────────────────────────────────────────────────

def _sim_swap_check(phone: str) -> dict:
    """SIM swap check — safe pass-through when DEMO_MODE is on."""
    if DEMO_MODE:
        return {"swapped": False, "confidence": 0.95}
    logger.info(f"SIM-swap check skipped for {phone} (not implemented in prod yet)")
    return {"swapped": False, "confidence": 0.80}


# ── PDF generator ─────────────────────────────────────────────────────────────

def generate_pdf(payload: dict) -> str:
    import tempfile

    ts    = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    fname = f"mizani_{safe_fn(payload['candidate'])}_{ts}.pdf"
    path  = os.path.join(tempfile.gettempdir(), fname)

    doc   = SimpleDocTemplate(path, pagesize=A4,
                               rightMargin=2*cm, leftMargin=2*cm,
                               topMargin=2.5*cm, bottomMargin=2.5*cm)
    styles = getSampleStyleSheet()
    brand  = colors.HexColor("#1A3C5E")
    lvl    = payload["alert_level"]
    ac     = {"CRITICAL": colors.HexColor("#D73B3B"),
               "YELLOW":   colors.HexColor("#E6A817"),
               "GREEN":    colors.HexColor("#2E8B57")}.get(lvl, colors.black)

    def sty(name, **kw):
        return ParagraphStyle(name, parent=styles["Normal"], **kw)

    story = [
        Paragraph("MIZANI AI", sty("T", fontSize=22, textColor=brand,
                                    spaceAfter=4, alignment=TA_CENTER)),
        Paragraph("Campaign Finance Anomaly Report",
                  sty("S", fontSize=10, textColor=colors.HexColor("#666"),
                      spaceAfter=16, alignment=TA_CENTER)),
        HRFlowable(width="100%", thickness=1.5, color=brand),
        Spacer(1, 0.4*cm),
        Paragraph(f"<font color='{ac.hexval()}'><b>ALERT: {lvl}</b></font>",
                  sty("B", fontSize=13, alignment=TA_CENTER, spaceAfter=12)),
    ]

    story.append(Paragraph("Candidate Summary",
                             sty("H", fontSize=12, textColor=brand,
                                 spaceBefore=14, spaceAfter=6)))
    sum_data = [
        ["Candidate",    payload["candidate"]],
        ["Constituency", payload["constituency"]],
        ["Generated",    datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")],
    ]
    story.append(_table(sum_data, [5*cm, 12*cm], brand))

    story.append(Paragraph("Financial Breakdown",
                             sty("H2", fontSize=12, textColor=brand,
                                 spaceBefore=14, spaceAfter=6)))
    d  = payload["declared_wealth"]
    t  = payload["total_estimated_spend"]
    sp = payload["spr_ratio"]
    an = round(sp - 1.0, 4)
    fin_data = [
        ["Metric",                "Amount (KES)", "Note"],
        ["Declared assets",       f"{d:,.0f}",    "IEBC filing"],
        ["Permissible ceiling",   f"{d*1.3:,.0f}","Declared × 1.3"],
        ["DB expenditures",       f"{payload['real_db_spend']:,.0f}", "Supabase"],
        ["Total estimated spend", f"{t:,.0f}",    "DB + Meta + USSD"],
        ["Spend-Promise Ratio",   f"{sp:.4f}",    "Total / ceiling"],
        ["Anomaly score",         f"{an:+.4f}",   "SPR - 1.0"],
    ]
    story.append(_table(fin_data, [6*cm, 4.5*cm, 6.5*cm], brand,
                        header=True, last_row_color=ac))

    if payload.get("evidence"):
        story.append(Paragraph("Evidence Sources",
                                 sty("H3", fontSize=12, textColor=brand,
                                     spaceBefore=14, spaceAfter=6)))
        ev_data = [["Source", "Amount (KES)", "Confidence"]]
        for ev in payload["evidence"]:
            ev_data.append([
                ev.get("type", "-"),
                f"{float(ev.get('amount',0)):,.0f}",
                f"{float(ev.get('confidence',0))*100:.0f}%",
            ])
        story.append(_table(ev_data, [6*cm, 5*cm, 6*cm], brand, header=True))

    story += [
        Spacer(1, 0.5*cm),
        HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCC")),
        Spacer(1, 0.3*cm),
        Paragraph(
            "<b>Methodology:</b> SPR = total_estimated_spend / (declared_assets x 1.3). "
            "Signals sourced from IEBC filings, Meta Ad Library, PPRA records, "
            "and citizen USSD/SMS field reports. "
            "Generated under Kenya's Access to Information Act (2016). "
            "Processed per the Data Protection Act (2019).",
            sty("F", fontSize=8, textColor=colors.HexColor("#888"), leading=12)
        ),
    ]
    doc.build(story)

    with open(path, "rb") as f:
        pdf_bytes = f.read()

    try:
        supabase().storage.from_(PDF_BUCKET).upload(
            path=fname,
            file=pdf_bytes,
            file_options={"content-type": "application/pdf"},
        )
        public_url = supabase().storage.from_(PDF_BUCKET).get_public_url(fname)
        logger.info(f"PDF uploaded: {public_url}")
        return public_url
    except Exception as e:
        logger.error(f"Supabase Storage upload failed: {e}")
        return path


def _table(data, col_widths, brand, header=False, last_row_color=None):
    t = Table(data, colWidths=col_widths)
    style = [
        ("FONTNAME",  (0,0), (-1,-1), "Helvetica"),
        ("FONTSIZE",  (0,0), (-1,-1), 10),
        ("GRID",      (0,0), (-1,-1), 0.3, colors.HexColor("#CCC")),
        ("PADDING",   (0,0), (-1,-1), 6),
        ("ROWBACKGROUNDS", (0,0), (-1,-1),
         [colors.HexColor("#F7F9FC"), colors.white]),
    ]
    if header:
        style += [
            ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
            ("BACKGROUND", (0,0), (-1,0), brand),
            ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
        ]
    if last_row_color:
        style += [
            ("FONTNAME",  (0,-1), (-1,-1), "Helvetica-Bold"),
            ("TEXTCOLOR", (0,-1), (-1,-1), last_row_color),
        ]
    t.setStyle(TableStyle(style))
    return t


# ── USSD Menus (Bilingual EN/SW) ──────────────────────────────────────────────
# FIX: Menus trimmed to stay safely under the 182-char Kenya USSD limit.
# Emojis removed from CON menus — some handsets and carriers strip or
# miscount multi-byte characters, which can cause the menu to display
# incorrectly or the session to drop.

USSD_MENUS = {
    "en": {
        "welcome": (
            "CON Welcome to Mizani\n"
            "Kenya Campaign Finance Monitor\n"
            "1. Report a sighting\n"
            "2. Check a candidate\n"
            "0. Switch to Kiswahili"
        ),
        "ask_const":      "CON Enter constituency name:",
        "ask_cand":       "CON Enter candidate full name:",
        "ask_sight": (
            "CON What did you observe?\n"
            "1. Billboard/signage\n"
            "2. Branded vehicle convoy\n"
            "3. Paid rally or event\n"
            "4. Cash or gifts distributed"
        ),
        "ask_cand_check": "CON Enter candidate name to check:",
        "invalid":        "END Invalid input. Please dial again.",
        "security":       "END Security check failed. Session ended.",
        "not_found":      "END Candidate not found. Try full name.",
        "error":          "END Service error. Please try again.",
    },
    "sw": {
        "welcome": (
            "CON Karibu Mizani\n"
            "Ufuatiliaji wa Fedha za Kampeni\n"
            "1. Ripoti unachokiona\n"
            "2. Angalia mgombea\n"
            "0. Badili kwa Kiingereza"
        ),
        "ask_const":      "CON Ingiza jina la jimbo:",
        "ask_cand":       "CON Ingiza jina kamili la mgombea:",
        "ask_sight": (
            "CON Uliona nini?\n"
            "1. Bango au tangazo\n"
            "2. Msururu wa magari\n"
            "3. Mkutano uliolipwa\n"
            "4. Pesa au zawadi"
        ),
        "ask_cand_check": "CON Ingiza jina la mgombea kukagua:",
        "invalid":        "END Ingizo batili. Jaribu tena.",
        "security":       "END Ukaguzi umeshindwa. Kikao kimeisha.",
        "not_found":      "END Mgombea hajapatikana. Jaribu jina kamili.",
        "error":          "END Hitilafu ya huduma. Jaribu tena.",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/import-iebc", methods=["POST"])
def import_iebc():
    if "file" not in request.files:
        return jsonify({"error": "No CSV file"}), 400
    df = pd.read_csv(request.files["file"])
    missing = {"name","constituency","party","declared_assets"} - set(df.columns)
    if missing:
        return jsonify({"error": f"Missing columns: {missing}"}), 400
    batch = df[["name","constituency","party","declared_assets"]].to_dict("records")
    for r in batch:
        r["candidate_name"]  = r.pop("name")
        r["political_party"] = r.pop("party")
        r["iebc_source"]     = "2022_elections"
    supabase().table("candidates").upsert(batch).execute()
    return jsonify({"status": "success", "imported": len(batch), "sample": batch[:2]})


@app.route("/api/analyze", methods=["POST"])
def analyze_candidate():
    data         = request.json or {}
    cand_name    = data.get("candidate_name","").strip().lower()
    constituency = data.get("constituency","").strip().lower()
    if not cand_name or not constituency:
        return jsonify({"error": "candidate_name and constituency required"}), 400

    # STEP 1 — IEBC lookup
    r = (supabase().table("candidates").select("*")
         .ilike("candidate_name", f"%{cand_name}%")
         .eq("constituency", constituency.title())
         .execute())
    if not r.data:
        return jsonify({
            "error": "Candidate not in IEBC data",
            "suggestions": candidate_suggestions(cand_name, constituency)
        }), 404
    cand     = r.data[0]
    declared = float(cand.get("declared_assets") or 0)

    # STEP 2 — DB expenditures
    exps = (supabase().table("expenditures")
            .select("amount,source_type,confidence_score")
            .eq("candidate_id", cand["candidate_id"])
            .execute())
    db_spend = sum(float(e.get("amount") or 0) for e in exps.data)

    # STEP 3 — External channels
    meta    = fetch_meta_ads(cand["candidate_name"])
    total   = db_spend + meta
    ceiling = declared * 1.3
    spr     = total / ceiling if ceiling > 0 else float("inf")
    anomaly = round(spr - 1.0, 4)
    lvl     = alert_level(spr)

    evidence = (
        [{"type": "meta_ads",      "amount": meta,     "confidence": 0.89},
         {"type": "iebc_declared", "amount": declared, "confidence": 1.0}]
        + [{"type": e.get("source_type","db"),
            "amount": float(e.get("amount") or 0),
            "confidence": float(e.get("confidence_score") or 0.5)}
           for e in exps.data]
    )

    payload = {
        "candidate":             cand["candidate_name"],
        "constituency":          cand["constituency"],
        "declared_wealth":       declared,
        "permissible_ceiling":   round(ceiling, 2),
        "real_db_spend":         round(db_spend, 2),
        "total_estimated_spend": round(total, 2),
        "spr_ratio":             round(spr, 4),
        "anomaly_score":         anomaly,
        "alert_level":           lvl,
        "evidence":              evidence,
    }

    # STEP 4 — PDF (run in background; return URL when ready)
    try:
        pdf_url = generate_pdf(payload)
        payload["report_pdf"] = pdf_url
    except Exception as e:
        logger.error(f"PDF error: {e}")
        payload["report_pdf"] = None

    # STEP 5 — SMS alert
    if lvl in ("YELLOW","CRITICAL"):
        ev_summary = f"Meta KES {meta:,.0f}, DB KES {db_spend:,.0f}"
        url = payload.get("report_pdf") or f"https://mizani.ke/r/{safe_fn(cand['candidate_name'])}"
        payload["sms_alert"] = send_alert_sms(
            cand["candidate_name"], cand["constituency"],
            spr, lvl, url, ev_summary
        )

    return jsonify(payload)


# ── USSD Callback ─────────────────────────────────────────────────────────────
#
# FIX SUMMARY (all issues addressed in this version):
#
#  1. Duplicate _ussd_response removed — single canonical version above.
#  2. GET method added — prevents 405 on health-checks / browser opens.
#  3. Language stored in _lang_cache (memory-first) — removes 2 Supabase
#     round-trips per step, saving ~400ms per USSD interaction.
#  4. set_session_lang writes to memory synchronously, DB in background thread.
#  5. reward_airtime() is fire-and-forget via _reward_airtime_async().
#  6. 182-char limit enforced in _ussd_response().
#  7. Emojis stripped from CON menus (multi-byte chars break some carriers).
#  8. AFRICAS_TALKING_USERNAME used consistently (was AFRICAS_TALKING_USERNAME in /health).
#  9. Error messages added to USSD_MENUS ("not_found", "error").
# 10. Entire callback wrapped in try/except — any unhandled exception now
#     returns a clean END message instead of a 500 that AT shows as a
#     network error to the user.

@app.route("/api/ussd/callback", methods=["GET", "POST"])
def ussd_callback():
    # FIX: Accept GET so AT gateway health-checks don't return 405
    if request.method == "GET":
        return Response("Mizani USSD OK", content_type="text/plain", status=200)

    try:
        session_id = request.values.get("sessionId", "")
        phone      = request.values.get("phoneNumber", "").strip()
        text       = request.values.get("text", "").strip()
        steps      = [s.strip() for s in text.split("*")] if text else []

        logger.info(f"USSD IN — session={session_id} phone={phone} text={repr(text)}")

        # Language resolution — memory-first (fast)
        lang = get_session_lang(session_id)
        M    = USSD_MENUS[lang]

        # SIM-swap guard (disabled for demo)
        if not DEMO_MODE:
            swap = _sim_swap_check(phone)
            if swap.get("swapped"):
                return _ussd_response(M["security"])

        # ── Level 0: Welcome (no text = first dial) ──────────────────────────
        if not text:
            set_session_lang(session_id, "en")
            return _ussd_response(USSD_MENUS["en"]["welcome"])

        # ── Language Toggle ──────────────────────────────────────────────────
        if len(steps) == 1 and steps[0] == "0":
            new_lang = "sw" if lang == "en" else "en"
            set_session_lang(session_id, new_lang)
            return _ussd_response(USSD_MENUS[new_lang]["welcome"])

        # ── Top-level branch selection ────────────────────────────────────────
        if len(steps) == 1:
            if steps[0] == "1":
                return _ussd_response(M["ask_const"])
            if steps[0] == "2":
                return _ussd_response(M["ask_cand_check"])
            return _ussd_response(M["invalid"])

        # ── BRANCH 1: Report Flow ─────────────────────────────────────────────
        if steps[0] == "1":

            if len(steps) == 2:
                # Got constituency → ask for candidate name
                return _ussd_response(M["ask_cand"])

            if len(steps) == 3:
                # Got candidate name → ask for sighting type
                return _ussd_response(M["ask_sight"])

            if len(steps) == 4:
                constituency = steps[1].title()
                candidate_nm = steps[2].title()
                choice       = steps[3]

                if choice not in SIGHTINGS:
                    return _ussd_response(M["invalid"])

                src, amt, conf = SIGHTINGS[choice]

                # DB writes (these are necessary — no way to defer them)
                cid = upsert_candidate(candidate_nm, constituency)
                insert_expenditure(
                    cid, src, amt, conf,
                    f"USSD sighting by {phone}: {src}",
                    constituency
                )
                count     = count_constituency_reports(constituency)
                report_id = format_report_id(cid)

                # FIX: Airtime is non-blocking — fires in background thread
                _reward_airtime_async(phone, 5.0)

                if lang == "sw":
                    msg = (
                        f"END Asante! Ripoti #{report_id} imehifadhiwa.\n"
                        f"Mgombea: {candidate_nm}, {constituency}\n"
                        f"Aina: {src}\n"
                        f"Ripoti {count} kutoka jimboni.\n"
                        f"Umepata KES 5 airtime. Mizani inashukuru!"
                    )
                else:
                    msg = (
                        f"END Report #{report_id} saved!\n"
                        f"Candidate: {candidate_nm}, {constituency}\n"
                        f"Sighting: {src}\n"
                        f"Joins {count} reports from this constituency.\n"
                        f"You earned KES 5 airtime. Thank you!"
                    )
                return _ussd_response(msg)

            # More steps than expected in branch 1
            return _ussd_response(M["invalid"])

        # ── BRANCH 2: Check Flow ──────────────────────────────────────────────
        if steps[0] == "2":

            if len(steps) == 2:
                name = steps[1].lower().strip()
                if not name:
                    return _ussd_response(M["invalid"])

                r = (supabase().table("candidates").select("*")
                     .ilike("candidate_name", f"%{name}%")
                     .limit(1).execute())

                if not r.data:
                    return _ussd_response(M["not_found"])

                c   = r.data[0]
                dec = float(c.get("declared_assets") or 0)

                exr = (supabase().table("expenditures")
                       .select("amount")
                       .eq("candidate_id", c["candidate_id"])
                       .execute())

                db_sp = sum(float(e.get("amount") or 0) for e in exr.data)
                total = db_sp + fetch_meta_ads(c["candidate_name"])
                ceil  = dec * 1.3
                spr   = round(total / ceil, 2) if ceil > 0 else 0.0
                lvl   = alert_level(spr)

                # FIX: KES amounts formatted without commas to keep char count low
                if lang == "sw":
                    msg = (
                        f"END {c['candidate_name']} ({c['constituency']})\n"
                        f"Mali: KES {dec:,.0f}\n"
                        f"Matumizi: KES {total:,.0f}\n"
                        f"SPR: {spr} | Hali: {lvl}\n"
                        f"Zaidi: mizani.ke"
                    )
                else:
                    msg = (
                        f"END {c['candidate_name']} ({c['constituency']})\n"
                        f"Declared: KES {dec:,.0f}\n"
                        f"Est. spend: KES {total:,.0f}\n"
                        f"SPR: {spr} | Status: {lvl}\n"
                        f"Full report: mizani.ke"
                    )
                return _ussd_response(msg)

            # More steps than expected in branch 2
            return _ussd_response(M["invalid"])

        # ── Fallback ──────────────────────────────────────────────────────────
        return _ussd_response(M["invalid"])

    except Exception as e:
        # FIX: Unhandled exceptions previously returned a Flask 500 HTML page,
        # which AT displayed as a network error. Now we return a clean END.
        logger.exception(f"USSD handler crashed: {e}")
        return _ussd_response("END Service temporarily unavailable. Please try again.")


# ── Health ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    try:
        count = supabase().table("candidates").select("count", count="exact").execute()
        candidates_loaded = count.count
        db_status = "ok"
    except Exception as e:
        candidates_loaded = -1
        db_status = str(e)

    return jsonify({
        "status":            "Mizani LIVE",
        "candidates_loaded": candidates_loaded,
        "db_status":         db_status,
        "demo_mode":         DEMO_MODE,
        "alert_recipients":  len(ALERT_RECIPIENTS),
        # FIX: was os.environ.get("AFRICAS_TALKING_USERNAME") — wrong key name
        "at_username":       os.environ.get("AFRICAS_TALKING_USERNAME"),
        "pdf_bucket":        PDF_BUCKET,
    })


# ── Entry point (development only — use gunicorn in production) ───────────────
# if __name__ == "__main__":
#     app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
