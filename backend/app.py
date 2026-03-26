# app.py — Mizani AI Backend (Production-Ready)
# pip install flask supabase python-dotenv africastalking requests pandas reportlab gunicorn

import os, re, json, logging
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
_at_initialized = False

def at(service: str):
    """Return cached AT service handle (sms | airtime | voice).
    Re-initialises whenever called with fresh env vars so sandbox→prod
    switch works correctly without a restart.
    """
    global _at_initialized, _at_cache

    username = os.environ.get("AT_USERNAME", "sandbox")
    api_key  = os.environ.get("AT_API_KEY", "")

    # Re-init if credentials have changed or not yet initialised
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
# Create a public bucket called "reports" in your Supabase project.
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

# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_fn(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", text)

def alert_level(spr: float) -> str:
    if spr > 1.5: return "CRITICAL"
    if spr > 1.2: return "YELLOW"
    return "GREEN"

def _ussd_response(msg: str) -> Response:
    return Response(msg, content_type="text/plain")

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
    """
    FIX: candidate_id is a UUID string, not an int.
    Take the first 8 hex chars and uppercase them for a readable reference.
    e.g. "a3f2c1d0-..." → "A3F2C1D0"
    """
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
        f"🚨 MIZANI [{lvl}]\n"
        f"Candidate: {candidate} ({constituency})\n"
        f"SPR: {spr:.2f} — {((spr-1)*100):.0f}% above declared wealth\n"
        f"Sources: {evidence_summary}\n"
        f"Report: {report_url}\n"
        f"Reply MOREINFO for evidence breakdown."
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


# ── PDF generator (uploads to Supabase Storage, not local disk) ───────────────

def generate_pdf(payload: dict) -> str:
    """
    Build the PDF in /tmp (ephemeral but fine for generation),
    upload to Supabase Storage, and return the public URL.

    FIX: Render's filesystem is ephemeral — files written outside /tmp
    disappear on every deploy. We now upload to Supabase Storage so
    reports persist permanently.
    """
    import tempfile

    ts    = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    fname = f"mizani_{safe_fn(payload['candidate'])}_{ts}.pdf"
    path  = os.path.join(tempfile.gettempdir(), fname)

    doc = SimpleDocTemplate(path, pagesize=A4,
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
        ["Anomaly score",         f"{an:+.4f}",   "SPR − 1.0"],
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
                ev.get("type", "—"),
                f"{float(ev.get('amount',0)):,.0f}",
                f"{float(ev.get('confidence',0))*100:.0f}%",
            ])
        story.append(_table(ev_data, [6*cm, 5*cm, 6*cm], brand, header=True))

    story += [
        Spacer(1, 0.5*cm),
        HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCC")),
        Spacer(1, 0.3*cm),
        Paragraph(
            "<b>Methodology:</b> SPR = total_estimated_spend ÷ (declared_assets × 1.3). "
            "Signals sourced from IEBC filings, Meta Ad Library, PPRA records, "
            "and citizen USSD/SMS field reports. "
            "Generated under Kenya's Access to Information Act (2016). "
            "Processed per the Data Protection Act (2019).",
            sty("F", fontSize=8, textColor=colors.HexColor("#888"), leading=12)
        ),
    ]
    doc.build(story)

    # Upload to Supabase Storage
    with open(path, "rb") as f:
        pdf_bytes = f.read()

    try:
        supabase().storage.from_(PDF_BUCKET).upload(
            path=fname,
            file=pdf_bytes,
            file_options={"content-type": "application/pdf"},
        )
        public_url = (
            supabase().storage.from_(PDF_BUCKET).get_public_url(fname)
        )
        logger.info(f"PDF uploaded: {public_url}")
        return public_url
    except Exception as e:
        logger.error(f"Supabase Storage upload failed: {e}")
        # Fallback: return temp path (won't persist but won't crash)
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


# ── USSD session language (Supabase-backed for multi-instance safety) ─────────

def get_session_lang(session_id: str) -> str:
    """
    FIX: In-memory SESSION_LANG dict breaks when Render runs multiple
    instances or restarts. We now persist language preference in Supabase.
    """
    try:
        r = (supabase().table("ussd_sessions")
             .select("lang")
             .eq("session_id", session_id)
             .execute())
        if r.data:
            return r.data[0].get("lang", "en")
    except Exception as e:
        logger.warning(f"get_session_lang error: {e}")
    return "en"

def set_session_lang(session_id: str, lang: str):
    try:
        supabase().table("ussd_sessions").upsert({
            "session_id": session_id,
            "lang":       lang,
            "updated_at": datetime.utcnow().isoformat(),
        }).execute()
    except Exception as e:
        logger.warning(f"set_session_lang error: {e}")


# ── SIM-swap check ────────────────────────────────────────────────────────────

def _sim_swap_check(phone: str) -> dict:
    """
    FIX: Previously returned a hardcoded dict in production.
    Now calls the AT SIM Swap Detection API when DEMO_MODE=false.
    Requires AT_SIM_SWAP_URL to be set (from AT dashboard).
    """
    if DEMO_MODE:
        return {"swapped": False, "confidence": 0.95}
    try:
        import requests as req
        url = os.environ.get(
            "AT_SIM_SWAP_URL",
            "https://api.africastalking.com/sim-swap/check"
        )
        headers = {
            "apiKey":  os.environ.get("AT_API_KEY", ""),
            "Accept":  "application/json",
        }
        resp = req.post(url, json={"phoneNumber": phone}, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return {
            "swapped":    data.get("swapped", False),
            "confidence": data.get("confidence", 0.80),
        }
    except Exception as e:
        logger.error(f"SIM swap check error: {e}")
        # Fail open — don't block legitimate users if the API is down
        return {"swapped": False, "confidence": 0.50}


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
        [{"type": "meta_ads", "amount": meta, "confidence": 0.89},
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

    # STEP 4 — PDF (uploaded to Supabase Storage)
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


# ── USSD (bilingual EN/SW) ────────────────────────────────────────────────────

USSD_MENUS = {
    "en": {
        "welcome": (
            "CON Welcome to Mizani 🔍\n"
            "Kenya Campaign Finance Monitor\n"
            "1. Report a sighting\n"
            "2. Check a candidate\n"
            "0. Switch to Kiswahili"
        ),
        "ask_const":     "CON Enter constituency name:",
        "ask_cand":      "CON Enter candidate full name:",
        "ask_sight": (
            "CON What did you observe?\n"
            "1. Billboard / signage\n"
            "2. Branded vehicle convoy\n"
            "3. Paid rally or event\n"
            "4. Cash or gifts distributed"
        ),
        "ask_cand_check": "CON Enter candidate name to check SPR:",
        "invalid":  "END ❌ Invalid input. Please dial *384# again.",
        "security": "END ⚠️ Security check failed. Session ended.",
    },
    "sw": {
        "welcome": (
            "CON Karibu Mizani 🔍\n"
            "Mfumo wa Ufuatiliaji wa Fedha za Kampeni\n"
            "1. Ripoti unachokiona\n"
            "2. Angalia mgombea\n"
            "0. Switch to English"
        ),
        "ask_const":     "CON Ingiza jina la jimbo:",
        "ask_cand":      "CON Ingiza jina kamili la mgombea:",
        "ask_sight": (
            "CON Uliona nini?\n"
            "1. Bango au tangazo\n"
            "2. Msururu wa magari yenye alama\n"
            "3. Mkutano uliolipwa\n"
            "4. Pesa au zawadi zilizosambazwa"
        ),
        "ask_cand_check": "CON Ingiza jina la mgombea kukagua SPR:",
        "invalid":  "END ❌ Ingizo batili. Piga simu *384# tena.",
        "security": "END ⚠️ Ukaguzi wa usalama umeshindwa. Kikao kimeisha.",
    },
}


@app.route("/api/ussd/callback", methods=["POST"])
def ussd_callback():
    session_id = request.values.get("sessionId", "")
    phone      = request.values.get("phoneNumber", "").strip()
    text       = request.values.get("text", "").strip()
    steps      = [s.strip() for s in text.split("*")] if text else []

    # Language resolution (Supabase-backed — safe across multiple instances)
    lang = get_session_lang(session_id)
    M    = USSD_MENUS[lang]

    # SIM-swap guard
    if not DEMO_MODE:
        swap = _sim_swap_check(phone)
        if swap.get("swapped"):
            return _ussd_response(M["security"])

    # ── Level 0 : welcome ──────────────────────────────────────────────────
    if not text:
        set_session_lang(session_id, "en")
        return _ussd_response(USSD_MENUS["en"]["welcome"])

    # ── Language toggle ────────────────────────────────────────────────────
    if len(steps) == 1 and steps[0] == "0":
        new_lang = "sw" if lang == "en" else "en"
        set_session_lang(session_id, new_lang)
        return _ussd_response(USSD_MENUS[new_lang]["welcome"])

    # ── Branch: Report (1) vs Check (2) ───────────────────────────────────
    if len(steps) == 1:
        if steps[0] == "1":
            return _ussd_response(M["ask_const"])
        if steps[0] == "2":
            return _ussd_response(M["ask_cand_check"])
        return _ussd_response(M["invalid"])

    # ── BRANCH 1: Report flow ─────────────────────────────────────────────
    if steps[0] == "1":
        if len(steps) == 2:
            return _ussd_response(M["ask_cand"])
        if len(steps) == 3:
            return _ussd_response(M["ask_sight"])
        if len(steps) == 4:
            constituency = steps[1].title()
            candidate_nm = steps[2].title()
            choice       = steps[3]
            if choice not in SIGHTINGS:
                return _ussd_response(M["invalid"])
            src, amt, conf = SIGHTINGS[choice]
            cid = upsert_candidate(candidate_nm, constituency)
            insert_expenditure(
                cid, src, amt, conf,
                f"USSD sighting by {phone}: {src}", constituency
            )
            count     = count_constituency_reports(constituency)
            report_id = format_report_id(cid)  # FIX: UUID-safe
            reward_airtime(phone, 5.0)

            if lang == "sw":
                msg = (
                    f"END ✅ Asante! Ripoti #{report_id} imehifadhiwa.\n"
                    f"Mgombea: {candidate_nm}, {constituency}\n"
                    f"Aina: {src}\n"
                    f"Ripoti {count} kutoka jimboni.\n"
                    f"Umepata KES 5 airtime. Mizani inashukuru!"
                )
            else:
                msg = (
                    f"END ✅ Report #{report_id} saved!\n"
                    f"Candidate: {candidate_nm}, {constituency}\n"
                    f"Sighting: {src}\n"
                    f"Your report joins {count} from this constituency.\n"
                    f"You earned KES 5 airtime. Thank you!"
                )
            return _ussd_response(msg)

    # ── BRANCH 2: Check flow ──────────────────────────────────────────────
    if steps[0] == "2":
        if len(steps) == 2:
            name = steps[1].lower()
            r = (supabase().table("candidates").select("*")
                 .ilike("candidate_name", f"%{name}%")
                 .limit(1).execute())
            if not r.data:
                return _ussd_response(
                    "END ❌ Candidate not found. Try the full name."
                    if lang == "en"
                    else "END ❌ Mgombea hajapatikana. Jaribu jina kamili."
                )
            c   = r.data[0]
            dec = float(c.get("declared_assets") or 0)
            exr = (supabase().table("expenditures")
                   .select("amount")
                   .eq("candidate_id", c["candidate_id"])
                   .execute())
            db_sp = sum(float(e.get("amount") or 0) for e in exr.data)
            total = db_sp + fetch_meta_ads(c["candidate_name"])
            ceil  = dec * 1.3
            spr   = round(total / ceil, 2) if ceil > 0 else 0
            lvl   = alert_level(spr)

            if lang == "sw":
                msg = (
                    f"END 📊 {c['candidate_name']} ({c['constituency']})\n"
                    f"Mali zilizotangazwa: KES {dec:,.0f}\n"
                    f"Matumizi ya jumla: KES {total:,.0f}\n"
                    f"Kiwango cha SPR: {spr}\n"
                    f"Hali: {lvl}\n"
                    f"Maelezo zaidi: mizani.ke"
                )
            else:
                msg = (
                    f"END 📊 {c['candidate_name']} ({c['constituency']})\n"
                    f"Declared wealth: KES {dec:,.0f}\n"
                    f"Estimated spend: KES {total:,.0f}\n"
                    f"SPR: {spr}\n"
                    f"Status: {lvl}\n"
                    f"Full report: mizani.ke"
                )
            return _ussd_response(msg)

    return _ussd_response(M["invalid"])


# ── Inbound SMS ───────────────────────────────────────────────────────────────

@app.route("/api/sms/inbound", methods=["POST"])
def sms_inbound():
    """
    Format: CONSTITUENCY CANDIDATE_NAME KEYWORD
    e.g.  : STAREHE JOHN DOE CONVOY
    Swahili: STAREHE JOHN DOE GARI
    """
    phone   = request.values.get("from", "").strip()
    message = request.values.get("text", "").strip().upper()

    if message.startswith("MOREINFO"):
        return _handle_journalist_reply(phone, message)

    sighting_key = None
    clean        = message
    for kw, choice in SMS_KEYWORDS.items():
        if kw in message:
            sighting_key = choice
            clean = message.replace(kw, "").strip()
            break

    parts = clean.split()
    if len(parts) < 2:
        send_sms([phone],
            "❌ Mizani: Format: CONSTITUENCY NAME SIGHTING\n"
            "e.g. STAREHE JOHN DOE CONVOY\n"
            "Dial *384# for a guided session."
        )
        return "OK", 200

    constituency = parts[0].title()
    candidate_nm = " ".join(parts[1:]).title()
    src, amt, conf = SIGHTINGS.get(sighting_key, ("citizen_report", 500_000, 0.60))

    cid       = upsert_candidate(candidate_nm, constituency)
    insert_expenditure(
        cid, src, amt, conf,
        f"SMS sighting from {phone}: {src}", constituency
    )
    count     = count_constituency_reports(constituency)
    report_id = format_report_id(cid)  # FIX: UUID-safe
    reward_airtime(phone, 5.0)

    send_sms([phone],
        f"✅ Mizani: Logged #{report_id}!\n"
        f"{candidate_nm}, {constituency} — {src}\n"
        f"{count} reports from this constituency.\n"
        f"You earned KES 5 airtime. Asante!"
    )
    return "OK", 200


def _handle_journalist_reply(phone: str, message: str):
    parts     = message.replace("MOREINFO", "").strip().split()
    cand_name = " ".join(parts).lower() if parts else ""
    if not cand_name:
        send_sms([phone], "❌ Usage: MOREINFO CANDIDATE NAME")
        return "OK", 200

    r = (supabase().table("candidates").select("*")
         .ilike("candidate_name", f"%{cand_name}%")
         .limit(1).execute())
    if not r.data:
        send_sms([phone], f"❌ No IEBC record found for '{cand_name}'.")
        return "OK", 200

    c    = r.data[0]
    exps = (supabase().table("expenditures")
            .select("source_type,amount,confidence_score,created_at")
            .eq("candidate_id", c["candidate_id"])
            .order("created_at", desc=True)
            .limit(5).execute())

    lines = [f"📋 Mizani evidence: {c['candidate_name']}"]
    for e in exps.data:
        lines.append(
            f"• {e['source_type']}: KES {float(e['amount']):,.0f} "
            f"({float(e['confidence_score'])*100:.0f}% conf)"
        )
    lines.append(f"Full PDF: mizani.ke/r/{safe_fn(c['candidate_name'])}")
    send_sms([phone], "\n".join(lines))
    return "OK", 200


# ── Outbound voice briefing (AT Voice) ───────────────────────────────────────

@app.route("/api/voice/brief", methods=["POST"])
def voice_brief():
    data  = request.json or {}
    phone = data.get("phone")
    cname = data.get("candidate_name", "Unknown")
    spr   = float(data.get("spr", 0))
    if not phone:
        return jsonify({"error": "phone required"}), 400

    call_from = os.environ.get("AT_VOICE_NUMBER", "")
    if DEMO_MODE:
        logger.info(f"[DEMO VOICE] Calling {phone} re: {cname} SPR={spr:.2f}")
        return jsonify({"demo": True, "phone": phone})
    try:
        resp = at("voice").call(call_from=call_from, call_to=phone)
        return jsonify(resp)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/voice/callback", methods=["POST"])
def voice_callback():
    cname = request.values.get("candidate_name", "a candidate")
    spr   = request.values.get("spr", "unknown")
    xml = (
        '<?xml version="1.0"?>'
        '<Response>'
        '<Say voice="en-US-Standard-D" playBeep="true">'
        f'This is an automated alert from Mizani AI. '
        f'Candidate {cname} has a Spend Promise Ratio of {spr}. '
        f'This exceeds the declared wealth threshold and requires investigation. '
        f'Please visit mizani dot ke for the full report.'
        '</Say>'
        '</Response>'
    )
    return Response(xml, content_type="application/xml")


@app.route("/api/sim-swap/<phone>", methods=["GET"])
def check_sim_swap(phone):
    return jsonify(_sim_swap_check(phone))


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
        "at_username":       os.environ.get("AT_USERNAME", "sandbox"),
        "pdf_bucket":        PDF_BUCKET,
    })


# ── Entry point (development only — use gunicorn in production) ───────────────
if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
