# === ANGUS™ Survey Bot — Booking Logic Upgrade v1 ===

from flask import Flask, render_template, request, jsonify
import requests
import datetime
import os
import urllib.parse
import time
import re

app = Flask(__name__)

# Airtable credentials
AIRTABLE_API_KEY = os.getenv('AIRTABLE_API_KEY')
AIRTABLE_BASE_ID = os.getenv('AIRTABLE_BASE_ID') 
AIRTABLE_RESPONSES_TABLE = os.getenv('AIRTABLE_TABLE_NAME') or "Survey Responses"
AIRTABLE_PROSPECTS_TABLE = os.getenv('AIRTABLE_PROSPECTS_TABLE') or "Prospects"
AIRTABLE_USERS_TABLE = os.getenv('AIRTABLE_USERS_TABLE') or "Users"

BASE_ID = AIRTABLE_BASE_ID
HQ_TABLE = AIRTABLE_PROSPECTS_TABLE
RESPONSES_TABLE = AIRTABLE_RESPONSES_TABLE
USERS_TABLE = AIRTABLE_USERS_TABLE

# GHL credentials
GHL_API_KEY = os.getenv('GHL_API_KEY')
GHL_LOCATION_ID = os.getenv('GHL_LOCATION_ID')
GHL_BASE_URL = "https://rest.gohighlevel.com/v1"

# Airtable helpers
def _h():
    return {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}

def _url(table, record_id=None):
    base = f"https://api.airtable.com/v0/{BASE_ID}/{urllib.parse.quote(table)}"
    return f"{base}/{record_id}" if record_id else base

# Get assignedUserId from Users table based on legacy code
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

# Create Prospect + Legacy Code
def create_prospect_and_legacy_code(email, phone):
    payload = {"fields": {"Prospect Email": email, "Prospect Phone": phone}}
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
        raise RuntimeError("AutoNum not found. Ensure Prospects has an Auto Number field named 'AutoNum'.")

    code_num = 1000 + int(auto)
    legacy_code = f"Legacy-X25-OP{code_num}"

    patch_payload = {"fields": {"Legacy Code": legacy_code}}
    requests.patch(_url(HQ_TABLE, rec_id), headers=_h(), json=patch_payload)

    return legacy_code, rec_id

# Push to GHL with assignedUserId
def push_to_ghl(email, phone, legacy_code, answers, record_id):
    try:
        clean_phone = re.sub(r"\D", "", phone)
        if len(clean_phone) == 10:
            clean_phone = "+1" + clean_phone
        elif len(clean_phone) == 11 and clean_phone.startswith("1"):
            clean_phone = "+" + clean_phone
        elif not clean_phone.startswith("+"):
            clean_phone = "+" + clean_phone

        print(f"📞 Cleaned phone for GHL: {clean_phone}")

        assigned_user_id = get_assigned_user_id(legacy_code)
        print(f"👤 Found assignedUserId: {assigned_user_id or '❌ None'}")

        url = f"{GHL_BASE_URL}/contacts"
        headers = {"Authorization": f"Bearer {GHL_API_KEY}", "Content-Type": "application/json"}

        payload = {
            "email": email,
            "phone": clean_phone,
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
            print("✅ Successfully synced to GHL")
            requests.patch(_url(HQ_TABLE, record_id), headers=_h(), 
                           json={"fields": {"Sync Status": "✅ Synced to GHL"}})
        else:
            error_msg = f"❌ Error {r.status_code}: {r.text}"
            print(error_msg)
            requests.patch(_url(HQ_TABLE, record_id), headers=_h(), 
                           json={"fields": {"Sync Status": error_msg}})
    except Exception as e:
        error_msg = f"❌ Exception: {str(e)}"
        print(error_msg)
        requests.patch(_url(HQ_TABLE, record_id), headers=_h(), 
                       json={"fields": {"Sync Status": error_msg}})

@app.route("/")
def index():
    return render_template("chat.html")

@app.route("/submit", methods=["POST"])
def submit():
    try:
        data = request.json
        email = data["email"]
        phone = data["phone"]
        answers = data["answers"]

        print(f"📩 Received survey: {email}, {phone}, {len(answers)} answers")

        while len(answers) < 6:
            answers.append("No response provided")

        legacy_code, prospect_id = create_prospect_and_legacy_code(email, phone)

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
            print("✅ Saved survey responses")
        else:
            print(f"❌ Error saving responses: {r3.text}")

        print("⏱ Waiting 60s before GHL sync...")
        time.sleep(60)

        push_to_ghl(email, phone, legacy_code, answers, prospect_id)

        return jsonify({"status": "success", "message": "Survey submitted. GHL sync in progress."})

    except Exception as e:
        print(f"🔥 Error in submit: {e}")
        return jsonify({"error": str(e), "status": "error"}), 500

@app.route("/health")
def health():
    return jsonify({"status": "healthy"})

if __name__ == "__main__":
    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
        print("❌ Missing Airtable env vars")
        exit(1)
    print("🚀 Starting Angus Survey Bot")
    app.run(debug=True, host='0.0.0.0', port=5000)
