# === ANGUS™ Survey Bot — EMAIL ONLY VERSION ===
# EXACT SAME WORKING LOGIC — ONLY THE 6 QUESTION FIELD MAPPINGS UPDATED

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
BASE_ID = AIRTABLE_BASE_ID
HQ_TABLE = AIRTABLE_PROSPECTS_TABLE
RESPONSES_TABLE = AIRTABLE_RESPONSES_TABLE

# ---------------------------------------------------------
# Airtable Helpers
# ---------------------------------------------------------
def _h():
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }

def _url(table, record_id=None):
    base = f"https://api.airtable.com/v0/{BASE_ID}/{urllib.parse.quote(table)}"
    return f"{base}/{record_id}" if record_id else base

# ---------------------------------------------------------
# Create Prospect record + assign Legacy Code (EMAIL ONLY)
# ---------------------------------------------------------
def create_prospect_and_legacy_code(email):
    payload = {
        "fields": {
            "Prospect Email": email
        }
    }

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

# ---------------------------------------------------------
# Routes
# ---------------------------------------------------------
@app.route("/")
def index():
    return render_template("chat.html")

@app.route("/submit", methods=["POST"])
def submit():
    try:
        data = request.json
        email = data["email"]
        answers = data["answers"]

        print(f"📩 Received survey: {email}, {len(answers)} answers")

        while len(answers) < 6:
            answers.append("No response provided")

        legacy_code, prospect_id = create_prospect_and_legacy_code(email)

        survey_payload = {
            "fields": {
                "Date Submitted": datetime.datetime.now().isoformat(),
                "Legacy Code": legacy_code,

                # IMPORTANT: field names stay EXACTLY as Airtable already has them
                "Q1 Real Reason for Change": answers[0],
                "Q2 Life/Work Starting Point": answers[1],
                "Q3 Weekly Bandwidth": answers[2],
                "Q4 Past Goal Killers": answers[3],
                "Q5 Work Style": answers[4],
                "Q6 Ready to Follow 90-Day Plan?": answers[5],

                "Prospects": [prospect_id]
            }
        }

        r3 = requests.post(_url(RESPONSES_TABLE), headers=_h(), json=survey_payload)
        if r3.status_code != 200:
            print(f"❌ Airtable Error: {r3.text}")
            return jsonify({"error": r3.text}), 500

        print("✅ Saved survey responses to Airtable")

        return jsonify({
            "status": "success",
            "legacy_code": legacy_code
        })

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

    print("🚀 Starting ANGUS Perfect 6 Bot (EMAIL ONLY)")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
