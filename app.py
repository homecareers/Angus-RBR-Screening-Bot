from flask import Flask, request, jsonify, render_template
import requests
import os
import datetime
import urllib.parse
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ---------------------- CONFIG ---------------------- #
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
HQ_TABLE = os.getenv("AIRTABLE_PROSPECTS_TABLE") or "Prospects"
RESPONSES_TABLE = os.getenv("AIRTABLE_SCREENING_TABLE") or "Survey Responses"
USERS_TABLE = os.getenv("AIRTABLE_USERS_TABLE") or "Users"

GHL_API_KEY = os.getenv("GHL_API_KEY")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID")
GHL_BASE_URL = "https://rest.gohighlevel.com/v1"

NEXTSTEP_URL = os.getenv("NEXTSTEP_URL") or "https://poweredbylegacycode.com/nextstep"


# ---------------------- HELPERS ---------------------- #
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


# ---------------------- OPERATOR LOOKUP ---------------------- #
def get_operator_info(ghl_user_id: str):
    try:
        formula = f"{{GHL User ID}} = '{ghl_user_id}'"
        search_url = _url(USERS_TABLE, params={"filterByFormula": formula, "maxRecords": 1})

        r = requests.get(search_url, headers=_h())
        r.raise_for_status()
        data = r.json()

        if data.get("records"):
            fields = data["records"][0].get("fields", {})
            return fields.get("Legacy Code"), fields.get("Email")

    except Exception as e:
        print(f"Error looking up operator info: {e}")

    return None, None


def update_prospect_with_operator_info(prospect_id: str, ghl_user_id: str):
    try:
        update_fields = {"GHL User ID": ghl_user_id}

        op_legacy_code, op_email = get_operator_info(ghl_user_id)

        if op_legacy_code:
            update_fields["Assigned Op Legacy Code"] = op_legacy_code

        if op_email:
            update_fields["Assigned Op Email"] = op_email

        r = requests.patch(
            _url(HQ_TABLE, prospect_id),
            headers=_h(),
            json={"fields": update_fields}
        )
        r.raise_for_status()

        print(f"Updated Prospect with GHL User ID: {ghl_user_id}, Op Legacy Code: {op_legacy_code}, Op Email: {op_email}")

    except Exception as e:
        print(f"Error updating prospect with operator info: {e}")


# ---------------------- PROSPECT HANDLING ---------------------- #
def get_or_create_prospect(email: str):
    formula = f"{{Prospect Email}} = '{email}'"
    search_url = _url(HQ_TABLE, params={"filterByFormula": formula, "maxRecords": 1})
    r = requests.get(search_url, headers=_h())
    r.raise_for_status()
    data = r.json()

    if data.get("records"):
        rec = data["records"][0]
        rec_id = rec["id"]
        fields = rec.get("fields", {})
        legacy_code = fields.get("Legacy Code")

        if not legacy_code:
            auto = fields.get("AutoNum")
            if auto is None:
                auto_data = requests.get(_url(HQ_TABLE, rec_id), headers=_h()).json()
                auto = auto_data.get("fields", {}).get("AutoNum")

            legacy_code = f"Legacy-X25-OP{1000 + int(auto)}"
            requests.patch(
                _url(HQ_TABLE, rec_id),
                headers=_h(),
                json={"fields": {"Legacy Code": legacy_code}},
            )

        return legacy_code, rec_id

    payload = {"fields": {"Prospect Email": email}}
    r = requests.post(_url(HQ_TABLE), headers=_h(), json=payload)
    r.raise_for_status()
    rec = r.json()
    rec_id = rec["id"]

    auto = rec.get("fields", {}).get("AutoNum")
    if auto is None:
        auto_data = requests.get(_url(HQ_TABLE, rec_id), headers=_h()).json()
        auto = auto_data.get("fields", {}).get("AutoNum")

    legacy_code = f"Legacy-X25-OP{1000 + int(auto)}"
    requests.patch(
        _url(HQ_TABLE, rec_id),
        headers=_h(),
        json={"fields": {"Legacy Code": legacy_code}},
    )

    return legacy_code, rec_id


# ---------------------- SAVE SNAPSHOT SURVEY (Q1–Q6) ---------------------- #
def save_snapshot_survey_to_airtable(legacy_code: str, prospect_id: str, answers: list):
    fields = {
        "Legacy Code": legacy_code,
        "Prospects": [prospect_id],
        "Date Submitted": datetime.datetime.utcnow().isoformat(),
        "Q1 Real Reason for Change": answers[0],
        "Q2 Life/Work Starting Point": answers[1],
        "Q3 Weekly Bandwidth": answers[2],
        "Q4 Past Goal Killers": answers[3],
        "Q5 Work Style": answers[4],
        "Q6 Ready to Follow 90-Day Plan?": answers[5],
    }

    r = requests.post(_url(RESPONSES_TABLE), headers=_h(), json={"fields": fields})
    r.raise_for_status()
    return r.json().get("id")


# ---------------------- GHL SYNC — SNAPSHOT SURVEY ---------------------- #
def push_snapshot_survey_to_ghl(email: str, answers: list, legacy_code: str, prospect_id: str):
    """
    Snapshot Survey Q1–Q6 via batch using field keys.
    legacy_code_id + atrid via old, proven per-field method.
    """
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

        if not contact:
            print(f"❌ No GHL contact found for email: {email}")
            return None

        ghl_id = contact.get("id")
        assigned = (
            contact.get("assignedUserId")
            or contact.get("userId")
            or contact.get("assignedTo")
        )

        # tag
        tag_response = requests.put(
            f"{GHL_BASE_URL}/contacts/{ghl_id}",
            headers=headers,
            json={"tags": ["snapshot survey submitted"]}
        )
        print(f"Tag update status: {tag_response.status_code}")

        # ---------- BATCH: Q1–Q6 ----------
        q1_to_q6_fields = {
            "q1_reason_for_business": str(answers[0]),
            "q2_lifework_starting_point": str(answers[1]),
            "q3_business_experience": str(answers[2]),
            "q4_startup_readiness": str(answers[3]),
            "q5_work_style": str(answers[4]),
            "q6_business_style_gem": str(answers[5]),
        }

        print("------ SENDING SNAPSHOT SURVEY Q1–Q6 CUSTOM FIELDS TO GHL ------")
        for key, val in q1_to_q6_fields.items():
            print(f"Field Key: {key} | Value: {str(val)[:60]}")
        print("----------------------------------------------------------------")

        batch_response = requests.put(
            f"{GHL_BASE_URL}/contacts/{ghl_id}",
            headers=headers,
            json={"customField": q1_to_q6_fields}
        )

        print(f"📊 Snapshot Survey Q1–Q6 batch status: {batch_response.status_code}")

        if batch_response.status_code != 200:
            print("❌ Snapshot Survey batch failed:")
            print(batch_response.text[:500])
        else:
            print("✅ Snapshot Survey batch SUCCESS")

        # ---------- LEGACY CODE + ATRID ----------
        field_updates = [
            {"legacy_code_id": legacy_code},
            {"atrid": prospect_id},
        ]

        for field_update in field_updates:
            try:
                field_name = list(field_update.keys())[0]
                field_value = field_update[field_name]

                print(f"➡ Updating {field_name} via legacy method")

                response = requests.put(
                    f"{GHL_BASE_URL}/contacts/{ghl_id}",
                    headers=headers,
                    json={"customField": field_update}
                )

                if response.status_code == 200:
                    print(f"✅ {field_name} updated (primary) -> {str(field_value)[:60]}")
                    continue

                alt_response = requests.put(
                    f"{GHL_BASE_URL}/contacts/{ghl_id}",
                    headers=headers,
                    json=field_update
                )

                if alt_response.status_code == 200:
                    print(f"✅ {field_name} updated (fallback)")
                else:
                    print(f"❌ {field_name} fallback failed: {alt_response.status_code}")
                    print(alt_response.text[:300])

            except Exception as e:
                print(f"Error updating field {field_update}: {e}")
                continue

        # operator info
        if assigned:
            update_prospect_with_operator_info(prospect_id, assigned)

        return assigned

    except Exception as e:
        print(f"❌ GHL Snapshot Survey Sync Error: {e}")
        return None


# ---------------------- ROUTES ---------------------- #
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

        while len(answers) < 6:
            answers.append("No response")

        legacy_code, prospect_id = get_or_create_prospect(email)

        save_snapshot_survey_to_airtable(legacy_code, prospect_id, answers)

        assigned_user_id = push_snapshot_survey_to_ghl(email, answers, legacy_code, prospect_id)

        if assigned_user_id:
            redirect_url = f"{NEXTSTEP_URL}?uid={assigned_user_id}"
        else:
            redirect_url = NEXTSTEP_URL

        return jsonify({"redirect_url": redirect_url})

    except Exception as e:
        print(f"Submit Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
