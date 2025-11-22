# === ANGUS™ Survey Bot — Full Routing Version (Perfect 6 Update) ===
# EMAIL ONLY VERSION - Hardened + Correct Q6 field name + Removed bad field

from flask import Flask, render_template, request, jsonify
import requests
import datetime
import os
import urllib.parse
import time

app = Flask(__name__)

# ---------------------------------------------------------
# Airtable Credentials / Tables
# ---------------------------------------------------------
AIRTABLE_API_KEY = os.getenv('AIRTABLE_API_KEY')
AIRTABLE_BASE_ID = os.getenv('AIRTABLE_BASE_ID')
AIRTABLE_RESPONSES_TABLE = os.getenv('AIRTABLE_TABLE_NAME') or "Survey Responses"
AIRTABLE_PROSPECTS_TABLE = os.getenv('AIRTABLE_PROSPECTS_TABLE') or "Prospects"
AIRTABLE_USERS_TABLE = os.getenv('AIRTABLE_USERS_TABLE') or "Users"

BASE_ID = AIRTABLE_BASE_ID
HQ_TABLE = AIRTABLE_PROSPECTS_TABLE
RESPONSES_TABLE = AIRTABLE_RESPONSES_TABLE
USERS_TABLE = AIRTABLE_USERS_TABLE

# ---------------------------------------------------------
# GoHighLevel Credentials
# ---------------------------------------------------------
GHL_API_KEY = os.getenv('GHL_API_KEY')
GHL_LOCATION_ID = os.getenv('GHL_LOCATION_ID')
GHL_BASE_URL = "https://rest.gohighlevel.com/v1"

# ---------------------------------------------------------
# Airtable Helpers
# ---------------------------------------------------------
def _h():
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }

def _url(table, record_id=None, params=None):
    base = f"https://api.airtable.com/v0/{BASE_ID}/{urllib.parse.quote(table)}"
    if record_id:
        return f"{base}/{record_id}"
    if params:
        return f"{base}?{urllib.parse.urlencode(params)}"
    return base

# ---------------------------------------------------------
# Get assignedUserId from Users table based on legacy code
# ---------------------------------------------------------
def get_assigned_user_id(legacy_code):
    try:
        formula = f"{{Legacy Code}} = '{legacy_code}'"
        params = {"filterByFormula": formula, "maxRecords": 1}
        r = requests.get(_url(USERS_TABLE, params=params), headers=_h())
        r.raise_for_status()

        records = r.json().get("records", [])
        if records and "fields" in records[0]:
            return records[0]["fields"].get("GHL User ID")

    except Exception as e:
        print(f"⚠ Error retrieving assignedUserId: {e}")

    return None

# ---------------------------------------------------------
# Find Prospect by email (reuse if exists)
# ---------------------------------------------------------
def find_prospect_by_email(email):
    formula = f"{{Prospect Email}} = '{email}'"
    params = {"filterByFormula": formula, "maxRecords": 1}
    r = requests.get(_url(HQ_TABLE, params=params), headers=_h())
    r.raise_for_status()
    records = r.json().get("records", [])
    return records[0] if records else None

# ---------------------------------------------------------
# Create Prospect + Legacy Code (email only)
# ---------------------------------------------------------
def create_prospect_and_legacy_code(email):
    payload = {"fields": {"Prospect Email": email}}

    r = requests.post(_url(HQ_TABLE), headers=_h(), json=payload)
    r.raise_for_status()
    rec = r.json()
    rec_id = rec["id"]

    auto = rec.get("fields", {}).get("AutoNum")
    if auto is None:
        r2 = requests.get(_url(HQ_TABLE, rec_id), headers=_h())
        r2.raise_for_status()
        auto = r2.json().get("fields", {}).get("AutoNum")

    if auto is None:
        raise RuntimeError("AutoNum not found in Prospects table.")

    code_num = 1000 + int(auto)
    legacy_code = f"Legacy-X25-OP{code_num}"

    patch_payload = {"fields": {"Legacy Code": legacy_code}}
    requests.patch(_url(HQ_TABLE, rec_id), headers=_h(), json=patch_payload)

    return legacy_code, rec_id

# ---------------------------------------------------------
# Get or Create Prospect
# ---------------------------------------------------------
def get_or_create_prospect(email):
    existing = find_prospect_by_email(email)
    if existing:
        fields = existing.get("fields", {})
        legacy_code = fields.get("Legacy Code")
        rec_id = existing["id"]

        if not legacy_code:
            auto = fields.get("AutoNum")
            if auto is None:
                r2 = requests.get(_url(HQ_TABLE, rec_id), headers=_h())
                r2.raise_for_status()
                auto = r2.json().get("fields", {}).get("AutoNum")
            legacy_code = f"Legacy-X25-OP{1000 + int(auto)}"
            requests.patch(_url(HQ_TABLE, rec_id), headers=_h(),
                           json={"fields": {"Legacy Code": legacy_code}})

        return legacy_code, rec_id

    return create_prospect_and_legacy_code(email)

# ---------------------------------------------------------
# Push to GHL (email only)
# ---------------------------------------------------------
def push_to_ghl(email, legacy_code, answers, record_id):
    try:
        assigned_user_id = get_assigned_user_id(legacy_code)
        print(f"👤 AssignedUserId: {assigned_user_id or 'None'}")

        url = f"{GHL_BASE_URL}/contacts"
        headers = {
            "Authorization": f"Bearer {GHL_API_KEY}",
            "Content-Type": "application/json"
        }

        payload = {
            "email": email,
            "locationId": GHL_LOCATION_ID,
            "customField": {
                "q1_real_reason_for_change": answers[0],
                "q2_life_work_starting_point": answers[1],
                "q3_weekly_bandwidth": answers[2],
                "q4_past_goal_killers": answers[3],
                "q5_work_style": answers[4],
                "q6_ready_to_follow_90_day_plan": answers[5],
                "legacy_code_id": legacy_code
            }
        }

        if assigned_user_id:
            payload["assignedUserId"] = assigned_user_id

        r = requests.post(url, headers=headers, json=payload)

        if r.status_code == 200:
            print("✅ Synced to GHL")
            requests.patch(
                _url(HQ_TABLE, record_id),
                headers=_h(),
                json={"fields": {"Sync Status": "Synced to GHL"}}
            )
        else:
            print(f"❌ GHL Error: {r.text}")
            requests.patch(
                _url(HQ_TABLE, record_id),
                headers=_h(),
                json={"fields": {"Sync Status": f'GHL ERR {r.status_code}'}}
            )

    except Exception as e:
        print(f"❌ GHL Exception: {str(e)}")
        requests.patch(
            _url(HQ_TABLE, record_id),
            headers=_h(),
            json={"fields": {"Sync Status": f'GHL EXC {str(e)}'}}
        )

# ---------------------------------------------------------
# Routes
# ---------------------------------------------------------
@app.route("/")
def index():
    return render_template("chat.html")

@app.route("/submit", methods=["POST"])
def submit():
    try:
        data = request.json or {}
        email = (data.get("email") or "").strip()
        answers = data.get("answers") or []

        if not email:
            return jsonify({"error": "Missing email"}), 400

        print(f"📩 Received survey: {email} | {len(answers)} answers")

        while len(answers) < 6:
            answers.append("No response provided")

        legacy_code, prospect_id = get_or_create_prospect(email)

        # -----------------------------------------------------
        # PERFECT 6 — EXACT Airtable Field Names
        # -----------------------------------------------------
        survey_payload = {
            "fields": {
                "Date Submitted": datetime.datetime.utcnow().isoformat(),
                "Legacy Code": legacy_code,

                "Q1 Real Reason for Change": answers[0],
                "Q2 Life/Work Starting Point": answers[1],
                "Q3 Weekly Bandwidth": answers[2],
                "Q4 Past Goal Killers": answers[3],
                "Q5 Work Style": answers[4],
                "Q6 Ready to Follow 90-Day Plan?": answers[5],

                "Prospects": [prospect_id]
                # ❌ Removed: "Prospect Email"
            }
        }

        r3 = requests.post(_url(RESPONSES_TABLE), headers=_h(), json=survey_payload)

        if r3.status_code != 200:
            print(f"❌ Airtable Error {r3.status_code}: {r3.text}")
            return jsonify({"error": r3.text}), 500

        print("✅ Saved to Airtable")

        print("⏱ Waiting 60s for GHL sync...")
        time.sleep(60)

        push_to_ghl(email, legacy_code, answers, prospect_id)

        return jsonify({
            "status": "success",
            "legacy_code": legacy_code
        })

    except Exception as e:
        print(f"🔥 Error: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"status": "healthy"})

# ---------------------------------------------------------
# Main
# ---------------------------------------------------------
if __name__ == "__main__":
    print("🚀 ANGUS Perfect 6 Bot • EMAIL ONLY • Fully Corrected (Q6 + Field Fix)")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
