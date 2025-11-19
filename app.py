from flask import Flask, render_template, request, jsonify
import requests
import datetime
import os
import urllib.parse
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Airtable creds
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_RESPONSES_TABLE = os.getenv("AIRTABLE_TABLE_NAME") or "Survey Responses"
AIRTABLE_PROSPECTS_TABLE = os.getenv("AIRTABLE_PROSPECTS_TABLE") or "Prospects"
AIRTABLE_USERS_TABLE = os.getenv("AIRTABLE_USERS_TABLE") or "Users"

BASE_ID = AIRTABLE_BASE_ID
RESPONSES_TABLE = AIRTABLE_RESPONSES_TABLE
HQ_TABLE = AIRTABLE_PROSPECTS_TABLE
USERS_TABLE = AIRTABLE_USERS_TABLE

# GHL creds
GHL_API_KEY = os.getenv("GHL_API_KEY")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID")
GHL_BASE_URL = "https://rest.gohighlevel.com/v1"

# Airtable helpers
def _h():
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }

def _url(table, record_id=None):
    base = f"https://api.airtable.com/v0/{BASE_ID}/{urllib.parse.quote(table)}"
    return f"{base}/{record_id}" if record_id else base

# Create Prospect + Legacy Code
def create_prospect_record(email):
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

# Sync to GHL
def push_to_ghl(email, legacy_code, answers, record_id):
    try:
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
        else:
            return None

        ghl_id = contact.get("id")

        # Assigned user ID
        assigned = (
            contact.get("assignedUserId")
            or contact.get("userId")
            or contact.get("assignedTo")
        )

        # Save ATRID
        requests.put(
            f"{GHL_BASE_URL}/contacts/{ghl_id}",
            headers=headers,
            json={"customField": {"atrid": record_id}},
        )

        # Save assigned ID to Airtable
        if assigned:
            requests.patch(
                _url(HQ_TABLE, record_id),
                headers=_h(),
                json={"fields": {"GHL User ID": assigned}},
            )

        # Update fields
        requests.put(
            f"{GHL_BASE_URL}/contacts/{ghl_id}",
            headers=headers,
            json={
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
            },
        )

        return assigned

    except Exception as e:
        print("GHL Sync Error:", e)
        return None

# ------------ THE IMPORTANT PART: JSON REDIRECT ------------
@app.route("/submit", methods=["POST"])
def submit():
    try:
        data = request.json or {}
        email = data.get("email", "").strip()
        answers = data.get("answers", [])

        if not email:
            return jsonify({"error": "Missing email"}), 400

        while len(answers) < 6:
            answers.append("No response")

        legacy_code, prospect_id = create_prospect_record(email)

        # Save survey
        requests.post(
            _url(RESPONSES_TABLE),
            headers=_h(),
            json={
                "fields": {
                    "Date Submitted": datetime.datetime.utcnow().isoformat(),
                    "Legacy Code": legacy_code,
                    "Q1 Reason for Business": answers[0],
                    "Q2 Time Commitment": answers[1],
                    "Q3 Business Experience": answers[2],
                    "Q4 Startup Readiness": answers[3],
                    "Q5 Confidence Level": answers[4],
                    "Q6 Business Style (GEM)": answers[5],
                    "Prospects": [prospect_id],
                }
            },
        )

        ghl_user_id = push_to_ghl(email, legacy_code, answers, prospect_id)

        # Build redirect URL
        base = "https://poweredbylegacycode.com/nextstep"
        redirect_url = f"{base}?uid={ghl_user_id}" if ghl_user_id else base

        # ⭐ RETURN JSON (NO BACKEND REDIRECT)
        return jsonify({"redirect_url": redirect_url})

    except Exception as e:
        print("Submit Error:", e)
        return jsonify({"error": str(e)}), 500

@app.route("/")
def index():
    return render_template("chat.html")

@app.route("/health")
def health():
    return jsonify({"status": "healthy"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
