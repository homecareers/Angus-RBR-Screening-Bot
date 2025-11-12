# === ANGUS™ Survey Bot — Elite Sync Version (Email Included) ===

from flask import Flask, render_template, request, jsonify
import requests
import datetime
import os
import urllib.parse
import time
import re

app = Flask(__name__)

# ---------------------------------------------------------
# Airtable Credentials
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
# GHL Credentials
# ---------------------------------------------------------
GHL_API_KEY = os.getenv('GHL_API_KEY')
GHL_LOCATION_ID = os.getenv('GHL_LOCATION_ID')
GHL_BASE_URL = "https://rest.gohighlevel.com/v1"


# ---------------------------------------------------------
# Airtable Helper Functions
# ---------------------------------------------------------

def _h():
    """Standard headers for Airtable API."""
    return {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}

def _url(table, record_id=None):
    base = f"https://api.airtable.com/v0/{BASE_ID}/{urllib.parse.quote(table)}"
    return f"{base}/{record_id}" if record_id else base


# ---------------------------------------------------------
# 1️⃣ Create Prospect Record (Now Stores Email)
# ---------------------------------------------------------
def create_prospect_record(email):
    """Create new Prospect with EMAIL + auto Legacy Code."""
    
    payload = {"fields": {"Prospect Email": email}}

    r = requests.post(_url(HQ_TABLE), headers=_h(), json=payload)
    r.raise_for_status()
    
    rec = r.json()
    rec_id = rec["id"]

    # Ensure AutoNum is loaded
    auto = rec.get("fields", {}).get("AutoNum")
    if auto is None:
        r2 = requests.get(_url(HQ_TABLE, rec_id), headers=_h())
        r2.raise_for_status()
        auto = r2.json().get("fields", {}).get("AutoNum")

    if auto is None:
        raise RuntimeError("AutoNum not found. Ensure Prospects has an Auto Number field named 'AutoNum'.")

    # Generate Legacy Code
    code_num = 1000 + int(auto)
    legacy_code = f"Legacy-X25-OP{code_num}"

    # Save Legacy Code back to Airtable
    patch_payload = {"fields": {"Legacy Code": legacy_code}}
    requests.patch(_url(HQ_TABLE, rec_id), headers=_h(), json=patch_payload)

    return legacy_code, rec_id


# ---------------------------------------------------------
# 2️⃣ Lookup Assigned User from Users Table
# ---------------------------------------------------------
def get_assigned_user_id(legacy_code):
    try:
        formula = f"{{Legacy Code}} = '{legacy_code}'"
        params = {"filterByFormula": formula}
        r = requests.get(_url(USERS_TABLE), headers=_h(), params=params)
        r.raise_for_status()

        records = r.json().get("records", [])
        if records and "fields" in records[0]:
            return records[0]["fields"].get("GHL User ID")

    except Exception as e:
        print(f"⚠️ Error retrieving assignedUserId: {e}")

    return None


# ---------------------------------------------------------
# 3️⃣ Push Final Survey + Legacy Code to GHL
# ---------------------------------------------------------
def push_to_ghl(email, legacy_code, answers, record_id):
    """Push contact + survey answers + legacy code to GoHighLevel."""

    try:
        assigned_user_id = get_assigned_user_id(legacy_code)

        url = f"{GHL_BASE_URL}/contacts"
        headers = {"Authorization": f"Bearer {GHL_API_KEY}", "Content-Type": "application/json"}

        payload = {
            "email": email,
            "locationId": GHL_LOCATION_ID,
            "customField": {
                "q1_reason_for_business": answers[0],
                "q2_time_commitment": answers[1],
                "q3_business_experience": answers[2],
                "q4_startup_readiness": answers[3],
                "q5_confidence_level": answers[4],
                "q6_business_style_gem": answers[5],
                "legacy_code_id": legacy_code
            }
        }

        if assigned_user_id:
            payload["assignedUserId"] = assigned_user_id

        r = requests.post(url, headers=headers, json=payload)

        if r.status_code == 200:
            print("✅ Synced to GHL")
            requests.patch(_url(HQ_TABLE, record_id), headers=_h(),
                           json={"fields": {"Sync Status": "✅ Synced to GHL"}})
        else:
            err = f"❌ GHL Error {r.status_code}: {r.text}"
            print(err)
            requests.patch(_url(HQ_TABLE, record_id), headers=_h(),
                           json={"fields": {"Sync Status": err}})

    except Exception as e:
        err = f"❌ Exception: {str(e)}"
        print(err)
        requests.patch(_url(HQ_TABLE, record_id), headers=_h(),
                       json={"fields": {"Sync Status": err}})


# ---------------------------------------------------------
# 4️⃣ Submit Route (Main Logic)
# ---------------------------------------------------------
@app.route("/submit", methods=["POST"])
def submit():
    try:
        data = request.json
        print("📩 Incoming Data:", data)

        email = data.get("email", "").strip()
        answers = data.get("answers", [])

        if not email:
            return jsonify({"error": "Missing email"}), 400

        # Ensure all 6 answers exist
        while len(answers) < 6:
            answers.append("No response provided")

        # 1. Create Prospect + Legacy Code
        legacy_code, prospect_id = create_prospect_record(email)

        # 2. Save survey responses
        survey_payload = {
            "fields": {
                "Date Submitted": datetime.datetime.now().isoformat(),
                "Legacy Code": legacy_code,
                "Q1 Reason for Business": answers[0],
                "Q2 Time Commitment": answers[1],
                "Q3 Business Experience": answers[2],
                "Q4 Startup Readiness": answers[3],
                "Q5 Confidence Level": answers[4],
                "Q6 Business Style (GEM)": answers[5],
                "Prospects": [prospect_id]
            }
        }
        r3 = requests.post(_url(RESPONSES_TABLE), headers=_h(), json=survey_payload)

        if r3.status_code == 200:
            print("✅ Survey saved to Airtable")
        else:
            print("❌ Airtable Error:", r3.text)

        # 3. Delay (user already redirected)
        print("⏱ Waiting 60 seconds before pushing to GHL...")
        time.sleep(60)

        # 4. Final GHL Sync
        push_to_ghl(email, legacy_code, answers, prospect_id)

        return jsonify({"status": "ok", "legacy_code": legacy_code})

    except Exception as e:
        print(f"🔥 Error in /submit: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------
# Basic Routes
# ---------------------------------------------------------
@app.route("/")
def index():
    return render_template("chat.html")

@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


# ---------------------------------------------------------
# Run Server
# ---------------------------------------------------------
if __name__ == "__main__":
    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
        print("❌ Missing Airtable env vars — cannot start.")
        exit(1)

    print("🚀 Starting Angus Survey Bot (Elite Sync Version)")
    app.run(debug=True, host='0.0.0.0', port=5000)
