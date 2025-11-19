# === ANGUS™ Survey Bot — FINAL PRODUCTION VERSION ===
# - Fully CORS-enabled for GoHighLevel iframe use
# - Iframe-safe redirect headers included
# - Airtable sync
# - GHL sync
# - Redirects parent window to /nextstep?uid=XXXX
# - Calendar routing endpoint
# ------------------------------------------------------

from flask import Flask, render_template, request, jsonify, redirect
from flask_cors import CORS
import requests
import datetime
import os
import urllib.parse

app = Flask(__name__)

# ⭐ Allow all origins so GHL iframe can POST safely
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# ---------------------------------------------------------
# Airtable Credentials
# ---------------------------------------------------------
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_RESPONSES_TABLE = os.getenv("AIRTABLE_TABLE_NAME") or "Survey Responses"
AIRTABLE_PROSPECTS_TABLE = os.getenv("AIRTABLE_PROSPECTS_TABLE") or "Prospects"
AIRTABLE_USERS_TABLE = os.getenv("AIRTABLE_USERS_TABLE") or "Users"

BASE_ID = AIRTABLE_BASE_ID
HQ_TABLE = AIRTABLE_PROSPECTS_TABLE
RESPONSES_TABLE = AIRTABLE_RESPONSES_TABLE
USERS_TABLE = AIRTABLE_USERS_TABLE

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
# Create Prospect Record + Legacy Code
# ---------------------------------------------------------
def create_prospect_record(email):
    payload = {"fields": {"Prospect Email": email}}
    r = requests.post(_url(HQ_TABLE), headers=_h(), json=payload)
    r.raise_for_status()
    rec = r.json()
    rec_id = rec["id"]

    # Get AutoNum
    auto = rec.get("fields", {}).get("AutoNum")
    if auto is None:
        r2 = requests.get(_url(HQ_TABLE, rec_id), headers=_h())
        auto = r2.json().get("fields", {}).get("AutoNum")

    legacy_code = f"Legacy-X25-OP{1000 + int(auto)}"

    # Save Code
    requests.patch(
        _url(HQ_TABLE, rec_id),
        headers=_h(),
        json={"fields": {"Legacy Code": legacy_code}},
    )

    return legacy_code, rec_id

# ---------------------------------------------------------
# Push to GHL + Return Assigned User ID
# ---------------------------------------------------------
def push_to_ghl(email, legacy_code, answers, record_id):
    assigned_user_id = None

    try:
        headers = {
            "Authorization": f"Bearer {GHL_API_KEY}",
            "Content-Type": "application/json",
        }

        lookup_resp = requests.get(
            f"{GHL_BASE_URL}/contacts/lookup",
            headers=headers,
            params={"email": email, "locationId": GHL_LOCATION_ID},
        )

        if lookup_resp.status_code != 200:
            return None

        data = lookup_resp.json()
        contact = None

        if "contacts" in data and data["contacts"]:
            contact = data["contacts"][0]
        elif "contact" in data:
            contact = data["contact"]

        ghl_id = contact.get("id")

        assigned_user_id = (
            contact.get("assignedUserId")
            or contact.get("userId")
            or contact.get("assignedTo")
        )

        # Save ATRID in GHL
        requests.put(
            f"{GHL_BASE_URL}/contacts/{ghl_id}",
            headers=headers,
            json={"customField": {"atrid": record_id}},
        )

        # Save Assigned User ID in Airtable
        if assigned_user_id:
            requests.patch(
                _url(HQ_TABLE, record_id),
                headers=_h(),
                json={"fields": {"GHL User ID": assigned_user_id}},
            )

        # Update GHL fields
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

    except Exception as e:
        print("GHL Sync Error:", e)

    return assigned_user_id

# ---------------------------------------------------------
# Submit Route (Iframe-safe redirect)
# ---------------------------------------------------------
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

        # Save survey answers
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

        assigned_user_id = push_to_ghl(email, legacy_code, answers, prospect_id)

        base = "https://poweredbylegacycode.com/nextstep"
        redirect_url = f"{base}?uid={assigned_user_id}" if assigned_user_id else base

        # ⭐ IFRAME-SAFE REDIRECT ⭐
        response = redirect(redirect_url, code=302)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Expose-Headers"] = "Location"
        response.headers["X-Frame-Options"] = "ALLOWALL"

        return response

    except Exception as e:
        print("Submit Error:", e)
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------
# Calendar Route
# ---------------------------------------------------------
@app.route("/calendar/<ghl_user_id>")
def calendar_lookup(ghl_user_id):
    try:
        formula = f"{{GHL User ID}} = '{ghl_user_id}'"
        r = requests.get(
            _url(USERS_TABLE),
            headers=_h(),
            params={"filterByFormula": formula},
        )

        data = r.json()
        if not data.get("records"):
            return "No matching user", 404

        calendar_url = data["records"][0]["fields"].get("Calendar Link")
        if not calendar_url:
            return "Calendar missing", 404

        return redirect(calendar_url)

    except Exception as e:
        return f"Error: {e}", 500

# ---------------------------------------------------------
# App Routes
# ---------------------------------------------------------
@app.route("/")
def index():
    return render_template("chat.html")

@app.route("/health")
def health():
    return jsonify({"status": "healthy"})

# ---------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
