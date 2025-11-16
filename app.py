# === ANGUS™ Survey Bot — Clean Version (No User Email Lookup) ===
# - Creates Prospect with unique Legacy Code
# - Searches for existing GHL contact by email
# - Retrieves the contact’s assigned GHL User ID
# - Stores that GHL User ID in Airtable Prospects
# - Updates GHL contact with survey answers + tag
# - Adds tag: "rbr screening survey submitted"
# - NO USER EMAIL LOOKUP ANYWHERE

from flask import Flask, render_template, request, jsonify
import requests
import datetime
import os
import urllib.parse
import time

app = Flask(__name__)

# ---------------------------------------------------------
# Airtable Credentials
# ---------------------------------------------------------
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_RESPONSES_TABLE = os.getenv("AIRTABLE_TABLE_NAME") or "Survey Responses"
AIRTABLE_PROSPECTS_TABLE = os.getenv("AIRTABLE_PROSPECTS_TABLE") or "Prospects"

BASE_ID = AIRTABLE_BASE_ID
HQ_TABLE = AIRTABLE_PROSPECTS_TABLE
RESPONSES_TABLE = AIRTABLE_RESPONSES_TABLE

# ---------------------------------------------------------
# GHL Credentials
# ---------------------------------------------------------
GHL_API_KEY = os.getenv("GHL_API_KEY")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID")
GHL_BASE_URL = "https://rest.gohighlevel.com/v1"

# ---------------------------------------------------------
# Airtable Helpers
# ---------------------------------------------------------
def _h():
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }

def _url(table, record_id=None):
    base = f"https://api.airtable.com/v0/{BASE_ID}/{urllib.parse.quote(table)}"
    return f"{base}/{record_id}" if record_id else base

# ---------------------------------------------------------
# 1️⃣ Create Prospect Record
# ---------------------------------------------------------
def create_prospect_record(email):
    """
    Create a new Prospect with:
      - Prospect Email
      - Auto-generated Legacy Code (Legacy-X25-OP####)
    """

    payload = {"fields": {"Prospect Email": email}}
    r = requests.post(_url(HQ_TABLE), headers=_h(), json=payload)
    r.raise_for_status()
    rec = r.json()
    rec_id = rec["id"]

    # Get AutoNum
    auto = rec.get("fields", {}).get("AutoNum")
    if auto is None:
        r2 = requests.get(_url(HQ_TABLE, rec_id), headers=_h())
        r2.raise_for_status()
        auto = r2.json().get("fields", {}).get("AutoNum")

    if auto is None:
        raise RuntimeError("AutoNum missing from Prospects table.")

    # Generate Legacy Code
    code_num = 1000 + int(auto)
    legacy_code = f"Legacy-X25-OP{code_num}"

    # Store Legacy Code
    requests.patch(
        _url(HQ_TABLE, rec_id),
        headers=_h(),
        json={"fields": {"Legacy Code": legacy_code}},
    )

    print(f"🧱 Created Prospect {rec_id} with Legacy Code {legacy_code}")
    return legacy_code, rec_id

# ---------------------------------------------------------
# 2️⃣ Push to GHL (User ID Only)
# ---------------------------------------------------------
def push_to_ghl(email, legacy_code, answers, record_id):
    """
    Look up existing GHL contact → get assigned GHL User ID → store ID in Airtable.
    Then update contact with survey answers + tag.
    """

    try:
        headers = {
            "Authorization": f"Bearer {GHL_API_KEY}",
            "Content-Type": "application/json",
        }

        # 1. Look up existing contact
        lookup_url = f"{GHL_BASE_URL}/contacts/lookup"
        lookup_params = {"email": email, "locationId": GHL_LOCATION_ID}

        lookup_resp = requests.get(lookup_url, headers=headers, params=lookup_params)

        if lookup_resp.status_code != 200:
            err = f"❌ GHL lookup failed for email {email}"
            print(err)
            requests.patch(
                _url(HQ_TABLE, record_id),
                headers=_h(),
                json={"fields": {"Sync Status": err}},
            )
            return

        contact_data = lookup_resp.json()

        # Extract contact
        if "contacts" in contact_data and contact_data["contacts"]:
            contact = contact_data["contacts"][0]
        elif "contact" in contact_data:
            contact = contact_data["contact"]
        else:
            contact = contact_data

        ghl_contact_id = contact.get("id")

        # Extract only user ID
        assigned_user_id = (
            contact.get("assignedUserId")
            or contact.get("userId")
            or contact.get("assignedTo")
            or contact.get("assigned_user_id")
        )

        print(f"📌 Found GHL Contact ID: {ghl_contact_id}")
        print(f"👤 Assigned User ID: {assigned_user_id}")

        # 2. Store ONLY the GHL User ID (no email)
        if assigned_user_id:
            requests.patch(
                _url(HQ_TABLE, record_id),
                headers=_h(),
                json={"fields": {"GHL User ID": assigned_user_id}},
            )
        else:
            print("⚠️ Contact has no assignedUserId")

        # 3. Update GHL contact with survey answers + tag
        update_url = f"{GHL_BASE_URL}/contacts/{ghl_contact_id}"
        update_payload = {
            "tags": ["rbr screening survey submitted"],
            "customField": {
                "q1_reason_for_business": answers[0],
                "q2_time_commitment": answers[1],
                "q3_business_experience": answers[2],
                "q4_startup_readiness": answers[3],
                "q5_confidence_level": answers[4],
                "q6_business_style_gem": answers[5],
                "legacy_code_id": legacy_code,
            },
        }

        update_resp = requests.put(update_url, headers=headers, json=update_payload)

        if update_resp.status_code == 200:
            print("✅ GHL contact updated with survey + tag")
            requests.patch(
                _url(HQ_TABLE, record_id),
                headers=_h(),
                json={"fields": {"Sync Status": "✅ Synced to GHL"}},
            )
        else:
            err = f"❌ GHL Update Error {update_resp.status_code}: {update_resp.text}"
            print(err)
            requests.patch(
                _url(HQ_TABLE, record_id),
                headers=_h(),
                json={"fields": {"Sync Status": err}},
            )

    except Exception as e:
        err = f"❌ Exception during GHL sync: {str(e)}"
        print(err)
        requests.patch(
            _url(HQ_TABLE, record_id),
            headers=_h(),
            json={"fields": {"Sync Status": err}},
        )

# ---------------------------------------------------------
# 3️⃣ Submit Route
# ---------------------------------------------------------
@app.route("/submit", methods=["POST"])
def submit():
    try:
        data = request.json or {}
        print("📩 Incoming:", data)

        email = (data.get("email") or "").strip()
        answers = data.get("answers", [])

        if not email:
            return jsonify({"error": "Missing email"}), 400

        # Ensure 6 answers
        while len(answers) < 6:
            answers.append("No response provided")

        # 1. Create Prospect
        legacy_code, prospect_id = create_prospect_record(email)

        # 2. Save Survey Responses
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
                "Prospects": [prospect_id],
            }
        }

        r3 = requests.post(_url(RESPONSES_TABLE), headers=_h(), json=survey_payload)

        if r3.status_code == 200:
            print("✅ Survey responses saved")
        else:
            print(f"❌ Airtable save error: {r3.status_code} {r3.text}")

        # 3. Background sync
        print("⏱ Waiting 60 seconds before GHL sync...")
        time.sleep(60)

        # 4. Update GHL
        push_to_ghl(email, legacy_code, answers, prospect_id)

        return jsonify({"status": "ok", "legacy_code": legacy_code})

    except Exception as e:
        print(f"🔥 Error in /submit: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------
# Debug Routes
# ---------------------------------------------------------
@app.route("/debug_contact/<email>")
def debug_contact(email):
    try:
        headers = {
            "Authorization": f"Bearer {GHL_API_KEY}",
            "Content-Type": "application/json",
        }
        lookup_url = f"{GHL_BASE_URL}/contacts/lookup"
        params = {"email": email, "locationId": GHL_LOCATION_ID}
        resp = requests.get(lookup_url, headers=headers, params=params)
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)})

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
        print("❌ Missing Airtable env vars.")
        exit(1)

    if not GHL_API_KEY or not GHL_LOCATION_ID:
        print("❌ Missing GHL env vars.")
        exit(1)

    print("🚀 Starting Angus Survey Bot (Clean Version — ID Only)")
    app.run(debug=True, host="0.0.0.0", port=5000)
