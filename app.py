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


# ---------------------- NEW: LOOKUP OPERATOR LEGACY CODE ---------------------- #
def get_operator_legacy_code(ghl_user_id: str):
    try:
        formula = f"{{GHL User ID}} = '{ghl_user_id}'"
        search_url = _url(USERS_TABLE, params={"filterByFormula": formula, "maxRecords": 1})

        r = requests.get(search_url, headers=_h())
        r.raise_for_status()
        data = r.json()

        if data.get("records"):
            user_record = data["records"][0]
            operator_legacy_code = user_record.get("fields", {}).get("Legacy Code")
            return operator_legacy_code

    except Exception as e:
        print(f"Error looking up operator Legacy Code: {e}")

    return None


def update_prospect_with_operator_info(prospect_id: str, ghl_user_id: str):
    try:
        update_fields = {"GHL User ID": ghl_user_id}

        operator_legacy_code = get_operator_legacy_code(ghl_user_id)
        if operator_legacy_code:
            update_fields["Assigned Op Legacy Code"] = operator_legacy_legacy_code

        r = requests.patch(
            _url(HQ_TABLE, prospect_id),
            headers=_h(),
            json={"fields": update_fields}
        )
        r.raise_for_status()

    except Exception as e:
        print(f"Error updating prospect with operator info: {e}")


# ---------------------- PROSPECT RECORD HANDLING ---------------------- #
def get_or_create_prospect(email: str):
    formula = f"{{Prospect Email}} = '{email}'"
    search_url = _url(HQ_TABLE, params={"filterByFormula": formula, "maxRecords": 1})
    r = requests.get(search_url, headers=_h())
    r.raise_for_status()
    data = r.json()

    if data.get("records"):
        rec = data["records"][0]
        rec_id = rec["id"]
        legacy_code = rec.get("fields", {}).get("Legacy Code")

        if not legacy_code:
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


# ---------------------- SAVE SURVEY ---------------------- #
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


# ---------------------- SYNC TO GHL ---------------------- #
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
            print(f"No contact found for email: {email}")
            return None

        ghl_id = contact.get("id")

        assigned = (
            contact.get("assignedUserId")
            or contact.get("userId")
            or contact.get("assignedTo")
        )

        # Tag the contact
        requests.put(
            f"{GHL_BASE_URL}/contacts/{ghl_id}",
            headers=headers,
            json={"tags": ["legacy screening submitted"]}
        )

        # CORRECTED FIELD MAPPING (Q5 NOW FIXED)
        field_updates = [
            {"q1_reason_for_business": answers[0]},
            {"q2_lifework_starting_point": answers[1]},
            {"q3_business_experience": answers[2]},
            {"q4_startup_readiness": answers[3]},
            {"q5_work_style": answers[4]},      # ✅ FIXED Q5
            {"legacy_code_id": legacy_code},
            {"atrid": prospect_id}
        ]

        for field_update in field_updates:
            try:
                requests.put(
                    f"{GHL_BASE_URL}/contacts/{ghl_id}",
                    headers=headers,
                    json={"customField": field_update}
                )
            except Exception as e:
                print(f"Error updating field {field_update}: {e}")
                continue

        if assigned:
            update_prospect_with_operator_info(prospect_id, assigned)

        return assigned

    except Exception as e:
        print(f"GHL Sync Error: {e}")
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
