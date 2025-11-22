# === ANGUS™ Perfect 6 Screening Bot — EMAIL ONLY — Full Routing + GHL Sync ===
# Commander-approved final build

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

    requests.patch(
        _url(HQ_TABLE, rec_id),
        headers=_h(),
        json={"fields": {"Legacy Code": legacy_code}}
    )

    return legacy_code, rec_id, auto

# ---------------------------------------------------------
# Assign Operator from Users (round-robin)
# Users fields (exact from your screenshot):
#   Legacy Code, GHL User ID, Email, Calendar Link
# ---------------------------------------------------------
def assign_operator_for_prospect(prospect_auto_num):
    r = requests.get(_url(USERS_TABLE), headers=_h())
    r.raise_for_status()
    users = r.json().get("records", [])

    if not users:
        return (None, None, None)

    # stable order by operator legacy code
    users_sorted = sorted(
        users,
        key=lambda u: u.get("fields", {}).get("Legacy Code", "")
    )

    idx = int(prospect_auto_num) % len(users_sorted)
    chosen = users_sorted[idx].get("fields", {})

    op_legacy_code = chosen.get("Legacy Code")
    op_email = chosen.get("Email")
    op_ghl_user_id = chosen.get("GHL User ID")

    return (op_legacy_code, op_email, op_ghl_user_id)

# ---------------------------------------------------------
# Get or Create Prospect + assign operator if new
# ---------------------------------------------------------
def get_or_create_prospect(email):
    existing = find_prospect_by_email(email)
    if existing:
        fields = existing.get("fields", {})
        legacy_code = fields.get("Legacy Code")
        rec_id = existing["id"]
        auto_num = fields.get("AutoNum")

        if not legacy_code:
            if auto_num is None:
                r2 = requests.get(_url(HQ_TABLE, rec_id), headers=_h())
                r2.raise_for_status()
                auto_num = r2.json().get("fields", {}).get("AutoNum")

            legacy_code = f"Legacy-X25-OP{1000 + int(auto_num)}"
            requests.patch(
                _url(HQ_TABLE, rec_id),
                headers=_h(),
                json={"fields": {"Legacy Code": legacy_code}}
            )

        return legacy_code, rec_id

    # create new prospect
    legacy_code, rec_id, auto_num = create_prospect_and_legacy_code(email)

    # assign operator
    op_legacy, op_email, op_ghl_id = assign_operator_for_prospect(auto_num)

    patch_fields = {}
    if op_ghl_id:
        patch_fields["GHL User ID"] = op_ghl_id
    if op_legacy:
        patch_fields["Assigned Op Legacy Code"] = op_legacy
    if op_email:
        patch_fields["Assigned Op Email"] = op_email

    if patch_fields:
        requests.patch(
            _url(HQ_TABLE, rec_id),
            headers=_h(),
            json={"fields": patch_fields}
        )

    return legacy_code, rec_id

# ---------------------------------------------------------
# Push to GHL (lookup -> update/create)
# ---------------------------------------------------------
def push_to_ghl(email, legacy_code, answers, prospect_record_id):
    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Content-Type": "application/json"
    }

    # lookup contact by email
    lookup = requests.get(
        f"{GHL_BASE_URL}/contacts/lookup",
        headers=headers,
        params={"email": email, "locationId": GHL_LOCATION_ID}
    ).json()

    contact = None
    if lookup.get("contacts"):
        contact = lookup["contacts"][0]
    elif lookup.get("contact"):
        contact = lookup["contact"]

    # pull assigned user id from Prospects
    pr = requests.get(_url(HQ_TABLE, prospect_record_id), headers=_h()).json()
    assigned_user_id = pr.get("fields", {}).get("GHL User ID")
    assigned_op_email = pr.get("fields", {}).get("Assigned Op Email")
    assigned_op_legacy = pr.get("fields", {}).get("Assigned Op Legacy Code")

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
            "legacy_code_id": legacy_code,

            # OPTIONAL (if you created these GHL custom fields)
            "assigned_op_email": assigned_op_email or "",
            "assigned_op_legacy_code": assigned_op_legacy or ""
        }
    }
    if assigned_user_id:
        payload["assignedUserId"] = assigned_user_id

    # update if exists else create
    if contact and contact.get("id"):
        cid = contact["id"]
        r = requests.put(
            f"{GHL_BASE_URL}/contacts/{cid}",
            headers=headers,
            json=payload
        )
    else:
        r = requests.post(
            f"{GHL_BASE_URL}/contacts",
            headers=headers,
            json=payload
        )

    if r.status_code in (200, 201):
        print("✅ GHL contact updated/created")
        requests.patch(
            _url(HQ_TABLE, prospect_record_id),
            headers=_h(),
            json={"fields": {"Sync Status": "✅ Synced to GHL"}}
        )
    else:
        print(f"❌ GHL sync failed: {r.status_code} {r.text}")
        requests.patch(
            _url(HQ_TABLE, prospect_record_id),
            headers=_h(),
            json={"fields": {"Sync Status": f"GHL ERR {r.status_code}"}}
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
            return jsonify({"status": "error", "error": "Missing email"}), 400

        print(f"📩 Received survey: {email} | answers={len(answers)}")

        # Guarantee 6 answers
        while len(answers) < 6:
            answers.append("No response provided")

        # Create/reuse prospect + assign op if new
        legacy_code, prospect_id = get_or_create_prospect(email)

        # -----------------------------------------------------
        # PERFECT 6 — EXACT Airtable Survey Responses fields
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
            }
        }

        r3 = requests.post(_url(RESPONSES_TABLE), headers=_h(), json=survey_payload)
        if r3.status_code != 200:
            print(f"❌ Airtable Save Failed {r3.status_code}: {r3.text}")
            return jsonify({"status": "error", "error": r3.text}), 500

        print("✅ Saved Perfect 6 to Airtable")

        # Wait for Airtable/automation stabilization
        print("⏱ Waiting 60s before GHL sync...")
        time.sleep(60)

        # Push to GHL
        push_to_ghl(email, legacy_code, answers, prospect_id)

        return jsonify({
            "status": "success",
            "legacy_code": legacy_code
        })

    except Exception as e:
        print(f"🔥 Error in submit: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"status": "healthy"})

# ---------------------------------------------------------
# Main
# ---------------------------------------------------------
if __name__ == "__main__":
    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
        print("❌ Missing Airtable env vars")
        exit(1)

    print("🚀 Starting ANGUS Perfect 6 Bot (EMAIL ONLY • Full Routing)")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
