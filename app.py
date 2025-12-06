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


# ---------------------- SAVE FIRST SURVEY (Q1–Q6) ---------------------- #
def save_screening_to_airtable(legacy_code: str, prospect_id: str, answers: list):
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


# ---------------------- GHL SYNC — TRUE BATCH FORMAT ---------------------- #
def push_screening_to_ghl(email: str, answers: list, legacy_code: str, prospect_id: str):
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

        # add tag
        tag_response = requests.put(
            f"{GHL_BASE_URL}/contacts/{ghl_id}",
            headers=headers,
            json={"tags": ["legacy screening submitted"]}
        )
        print(f"Tag update status: {tag_response.status_code}")

        # -----------------------
        # TRUE BATCH PAYLOAD
        # -----------------------
        custom_fields_payload = [
            {"id": "UNyQ5ZdjLihqjycS22lc", "value": answers[0]},  # Q1
            {"id": "lDkz6Qsg5ZjLMAXaK381", "value": answers[1]},  # Q2
            {"id": "LQkf4Bzx5ZW8y3aPF6b7", "value": answers[2]},  # Q3
            {"id": "Vk3oIWdHChpQPlX201fZ", "value": answers[3]},  # Q4
            {"id": "dCDnpK3iAY3k8prEmJs7", "value": answers[4]},  # Q5
            {"id": "4MwUuyWamknHDzYeko6L", "value": answers[5]},  # Q6
            {"id": "legacy_code_id", "value": legacy_code},
            {"id": "atrid", "value": prospect_id},
        ]

        print("------ SENDING CUSTOM FIELDS TO GHL ------")
        for f in custom_fields_payload:
            print(f"Field ID: {f['id']} | Value: {str(f['value'])[:60]}")
        print("-------------------------------------------")

        payload = {"customFields": custom_fields_payload}

        print(f"📦 Sending TRUE batch update to GHL for contact {ghl_id}")
        batch_response = requests.put(
            f"{GHL_BASE_URL}/contacts/{ghl_id}",
            headers=headers,
            json=payload
        )

        print(f"📊 Batch update status: {batch_response.status_code}")
        if batch_response.status_code != 200:
            print("❌ Batch update failed:")
            print(batch_response.text[:500])
        else:
            print("✅ Batch update SUCCESS")

        if assigned:
            update_prospect_with_operator_info(prospect_id, assigned)

        return assigned

    except Exception as e:
        print(f"❌ GHL Screening Sync Error: {e}")
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

        save_screening_to_airtable(legacy_code, prospect_id, answers)

        assigned_user_id = push_screening_to_ghl(email, answers, legacy_code, prospect_id)

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
