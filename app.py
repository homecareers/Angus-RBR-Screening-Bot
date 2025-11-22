# === ANGUS™ Perfect 6 Screening Bot — RBR v2 (Airtable + GHL Sync) ===

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import requests
import os
import datetime
import urllib.parse

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ---------------------- CONFIG ---------------------- #

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

HQ_TABLE = os.getenv("AIRTABLE_PROSPECTS_TABLE") or "Prospects"
RESPONSES_TABLE = os.getenv("AIRTABLE_SCREENING_TABLE") or "Survey Responses"

GHL_API_KEY = os.getenv("GHL_API_KEY")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID")
GHL_BASE_URL = "https://rest.gohighlevel.com/v1"

NEXTSTEP_URL = os.getenv("NEXTSTEP_URL") or "https://poweredbylegacycode.com/nextstep"


def _h():
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }


def _url(table, rec_id=None, params=None):
    base = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{urllib.parse.quote(table)}"
    if rec_id:
        return f"{base}/{rec_id}"
    if params:
        return f"{base}?{urllib.parse.urlencode(params)}"
    return base


# ---------------------- PROSPECT RECORD HANDLING ---------------------- #

def get_or_create_prospect(email: str):
    """
    Search for Prospect by email. If exists → return it.
    If not → create new + assign Legacy Code.
    """
    formula = f"{{Prospect Email}} = '{email}'"
    search_url = _url(HQ_TABLE, params={"filterByFormula": formula, "maxRecords": 1})

    r = requests.get(search_url, headers=_h())
    r.raise_for_status()
    data = r.json()

    # FOUND EXISTING PROSPECT
    if data.get("records"):
        rec = data["records"][0]
        rec_id = rec["id"]
        legacy_code = rec.get("fields", {}).get("Legacy Code")

        if not legacy_code:
            auto = rec.get("fields", {}).get("AutoNum")
            if auto is None:
                r2 = requests.get(_url(HQ_TABLE, rec_id), headers=_h())
                auto = r2.json().get("fields", {}).get("AutoNum")

            legacy_code = f"Legacy-X25-OP{1000 + int(auto)}"

            requests.patch(
                _url(HQ_TABLE, rec_id),
                headers=_h(),
                json={"fields": {"Legacy Code": legacy_code}},
            )

        return legacy_code, rec_id

    # CREATE NEW PROSPECT
    payload = {"fields": {"Prospect Email": email}}
    r = requests.post(_url(HQ_TABLE), headers=_h(), json=payload)
    r.raise_for_status()
    rec = r.json()
    rec_id = rec["id"]

    auto = rec.get("fields", {}).get("AutoNum")
    if auto is None:
        r2 = requests.get(_url(HQ_TABLE, rec_id), headers=_h())
        auto = r2.json().get("fields", {}).get("AutoNum")

    legacy_code = f"Legacy-X25-OP{1000 + int(auto)}"

    requests.patch(
        _url(HQ_TABLE, rec_id),
        headers=_h(),
        json={"fields": {"Legacy Code": legacy_code}},
    )

    return legacy_code, rec_id


# ---------------------- SAVE SCREENING (Perfect 6) ---------------------- #

def save_screening_to_airtable(legacy_code: str, prospect_id: str, email: str, answers: list):
    """
    Writes Perfect 6 into the 'Survey Responses' table.
    """
    fields = {
        "Legacy Code": legacy_code,
        "Prospects": [prospect_id],
        "Date Submitted": datetime.datetime.utcnow().isoformat(),
        "Prospect Email": email,

        # ✅ NEW Perfect 6 field names (Round 1)
        "Q1 Real Reason for Change (Round 1)": answers[0],
        "Q2 Life/Work Starting Point (Round 1)": answers[1],
        "Q3 Weekly Bandwidth (Round 1)": answers[2],
        "Q4 Past Goal Killers (Round 1)": answers[3],
        "Q5 Work Style (Round 1)": answers[4],
        "Q6 Ready to Follow 90-Day Plan? (Round 1)": answers[5],
    }

    r = requests.post(_url(RESPONSES_TABLE), headers=_h(), json={"fields": fields})
    r.raise_for_status()
    return r.json().get("id")


# ---------------------- SYNC TO GHL ---------------------- #

def push_screening_to_ghl(email: str, answers: list, legacy_code: str):
    """
    Updates the GHL contact record with Perfect 6 answers + legacy code.
    """
    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Content-Type": "application/json",
    }

    lookup = requests.get(
        f"{GHL_BASE_URL}/contacts/lookup",
        headers=headers,
        params={"email": email, "locationId": GHL_LOCATION_ID},
    ).json()

    contact = None
    if "contacts" in lookup and lookup["contacts"]:
        contact = lookup["contacts"][0]
    elif "contact" in lookup:
        contact = lookup["contact"]

    if not contact:
        return None

    ghl_id = contact.get("id")
    assigned = (
        contact.get("assignedUserId")
        or contact.get("userId")
        or contact.get("assignedTo")
    )

    update_payload = {
        "tags": ["legacy screening submitted", "rbr perfect6 completed"],
        "customField": {
            # ✅ NEW Perfect 6 custom field keys
            "q1_real_reason_for_change": answers[0],
            "q2_life_work_starting_point": answers[1],
            "q3_weekly_bandwidth": answers[2],
            "q4_past_goal_killers": answers[3],
            "q5_work_style": answers[4],
            "q6_ready_to_follow_90_day_plan": answers[5],

            "legacy_code_id": legacy_code,
        },
    }

    requests.put(
        f"{GHL_BASE_URL}/contacts/{ghl_id}",
        headers=headers,
        json=update_payload,
    )

    return assigned


# ---------------------- ROUTES ---------------------- #

@app.route("/")
def index():
    return render_template("chat.html")


@app.route("/submit", methods=["POST"])
def submit():
    try:
        data = request.json
        email = data.get("email", "").strip()
        answers = data.get("answers", [])

        if not email:
            return jsonify({"status": "error", "error": "Missing email"}), 400

        # pad to 6 if needed
        while len(answers) < 6:
            answers.append("No response provided")

        legacy_code, prospect_id = get_or_create_prospect(email)

        save_screening_to_airtable(legacy_code, prospect_id, email, answers)
        push_screening_to_ghl(email, answers, legacy_code)

        return jsonify({
            "status": "success",
            "legacy_code": legacy_code,
            "redirect_url": NEXTSTEP_URL
        })

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


if __name__ == "__main__":
    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
        print("❌ Missing Airtable env vars")
        exit(1)

    print("🚀 Starting ANGUS Perfect 6 Bot (RBR v2)")
    app.run(debug=True, host="0.0.0.0", port=5000)
