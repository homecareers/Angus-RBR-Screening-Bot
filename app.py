# === ANGUS™ Survey Bot — Elite OP Routing Version ===
# - Creates Prospect with unique Legacy Code
# - Automatically assigns an OP using Users table (round-robin)
# - Stores Assigned Op Legacy Code + Email + GHL User ID on Prospect
# - Pushes contact + survey answers + Legacy Code to GHL
# - Uses assignedUserId so each Prospect is owned by the correct GHL user
# - TAG: "rbr screening survey submitted"

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
# Users Table: Fetch + Round-Robin OP Assignment
# ---------------------------------------------------------
def get_all_users():
    """
    Loads operator rows from Users table.
    Requires fields:
      - Legacy Code
      - GHL User ID
      - Assigned Op Email / OP Email / Email
    """
    users = []
    offset = None
    try:
        while True:
            params = {"offset": offset} if offset else {}
            r = requests.get(_url(USERS_TABLE), headers=_h(), params=params)
            r.raise_for_status()
            data = r.json()

            for rec in data.get("records", []):
                fields = rec.get("fields", {})
                users.append(
                    {
                        "record_id": rec.get("id"),
                        "legacy_code": fields.get("Legacy Code"),
                        "ghl_user_id": fields.get("GHL User ID"),
                        "email": (
                            fields.get("Assigned Op Email")
                            or fields.get("OP Email")
                            or fields.get("Email")
                        ),
                    }
                )

            offset = data.get("offset")
            if not offset:
                break

    except Exception as e:
        print(f"⚠️ Error loading Users table: {e}")

    print(f"👥 Loaded {len(users)} OP user(s).")
    return users


def choose_op_for_autonum(auto_num):
    """Select an OP in round-robin order based on AutoNum."""
    users = get_all_users()
    if not users:
        print("⚠️ No OP users in Users table.")
        return None

    try:
        idx = (int(auto_num) - 1) % len(users)
    except Exception:
        idx = 0

    op = users[idx]
    print(
        f"👤 Assigned OP (AutoNum={auto_num}): "
        f"Legacy={op.get('legacy_code')} | GHL User ID={op.get('ghl_user_id')} | Email={op.get('email')}"
    )
    return op

# ---------------------------------------------------------
# 1️⃣ Create Prospect Record
# ---------------------------------------------------------
def create_prospect_record(email):
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
        raise RuntimeError("AutoNum missing from Prospects table.")

    code_num = 1000 + int(auto)
    legacy_code = f"Legacy-X25-OP{code_num}"

    assigned_op = choose_op_for_autonum(auto)

    print("🔍 assigned_op dict:", assigned_op)

    fields_to_patch = {"Legacy Code": legacy_code}
    if assigned_op:
        if assigned_op.get("legacy_code"):
            fields_to_patch["Assigned Op Legacy Code"] = assigned_op["legacy_code"]
        if assigned_op.get("email"):
            fields_to_patch["Assigned Op Email"] = assigned_op["email"]
        if assigned_op.get("ghl_user_id"):
            # This should match the 'GHL User ID' field in Prospects
            fields_to_patch["GHL User ID"] = assigned_op["ghl_user_id"]

    print("🧾 Patching Prospect with fields:", fields_to_patch)

    requests.patch(_url(HQ_TABLE, rec_id), headers=_h(), json={"fields": fields_to_patch})

    print(f"🧱 Created Prospect {rec_id} with Legacy Code {legacy_code}")
    return legacy_code, rec_id, assigned_op

# ---------------------------------------------------------
# 2️⃣ Push to GHL (Tag + assignment)
# ---------------------------------------------------------
def push_to_ghl(email, legacy_code, answers, record_id, assigned_op=None):
    try:
        url = f"{GHL_BASE_URL}/contacts"
        headers = {
            "Authorization": f"Bearer {GHL_API_KEY}",
            "Content-Type": "application/json",
        }

        payload = {
            "email": email,
            "locationId": GHL_LOCATION_ID,
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

        if assigned_op and assigned_op.get("ghl_user_id"):
            payload["assignedUserId"] = assigned_op["ghl_user_id"]
            print(f"📌 GHL assignedUserId = {assigned_op['ghl_user_id']}")
        else:
            print("⚠️ No GHL User ID found for OP. Contact will be unassigned.")

        r = requests.post(url, headers=headers, json=payload)

        if r.status_code == 200:
            print("✅ Synced to GHL")
            requests.patch(
                _url(HQ_TABLE, record_id),
                headers=_h(),
                json={"fields": {"Sync Status": "✅ Synced to GHL"}},
            )
        else:
            err = f"❌ GHL Error {r.status_code}: {r.text}"
            print(err)
            requests.patch(
                _url(HQ_TABLE, record_id),
                headers=_h(),
                json={"fields": {"Sync Status": err}},
            )

    except Exception as e:
        err = f"❌ Exception during GHL sync: {str(e)}"
        print(err)
        try:
            requests.patch(
                _url(HQ_TABLE, record_id),
                headers=_h(),
                json={"fields": {"Sync Status": err}},
            )
        except:
            pass

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

        while len(answers) < 6:
            answers.append("No response provided")

        legacy_code, prospect_id, assigned_op = create_prospect_record(email)

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
            print(f"❌ Airtable error: {r3.status_code} {r3.text}")

        print("⏱ Waiting 60 seconds before GHL sync...")
        time.sleep(60)

        push_to_ghl(email, legacy_code, answers, prospect_id, assigned_op)

        return jsonify({"status": "ok", "legacy_code": legacy_code})

    except Exception as e:
        print(f"🔥 Error in /submit: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------
# 4️⃣ Debug Route — See what the app sees from Users
# ---------------------------------------------------------
@app.route("/debug_users")
def debug_users():
    users = get_all_users()
    return jsonify(users)

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
        print("❌ Missing Airtable environment variables.")
        exit(1)

    print("🚀 Starting Angus Survey Bot (with /debug_users)")
    app.run(debug=True, host="0.0.0.0", port=5000)
