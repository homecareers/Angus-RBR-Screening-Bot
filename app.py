from flask import Flask, request, jsonify
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


# ---------------------- PROSPECT RECORD HANDLING ---------------------- #

def get_or_create_prospect(email: str):
    """
    Search for Prospect by email. If exists → return it.
    If not → create new + assign Legacy Code.
    """

    formula = f"{{Prospect Email}} = '{email}'"
    search_url = _url(HQ_TABLE, params={"filterByFormula": formula, "maxRecords": 1})

    r = requests.get(search_url, headers=_h())
    r.raise_for_status()
    data = r.json()

    # ------------------ FOUND EXISTING PROSPECT ------------------ #
    if data.get("records"):
        rec = data["records"][0]
        rec_id = rec["id"]
        legacy_code = rec.get("fields", {}).get("Legacy Code")

        # Missing LC? Generate one.
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

    # ------------------ CREATE NEW PROSPECT ------------------ #
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


# ---------------------- SAVE SURVEY (Q1–Q6) ---------------------- #

def save_screening_to_airtable(legacy_code: str, prospect_id: str, email: str, answers: list):
    """
    Writes Q1–Q6 into the 'Survey Responses' table.
    """

    fields = {
        "Legacy Code": legacy_code,
        "Prospects": [prospect_id],
        "Date Submitted": datetime.datetime.utcnow().isoformat(),
        "Prospect Email": email,
        "Q1 Reason for Business": answers[0],
        "Q2 Time Commitment": answers[1],
        "Q3 Business Experience": answers[2],
        "Q4 Startup Readiness": answers[3],
        "Q5 Confidence Level": answers[4],
        "Q6 Business Style (GEM)": answers[5],
    }

    r = requests.post(_url(RESPONSES_TABLE), headers=_h(), json={"fields": fields})
    r.raise_for_status()
    return r.json().get("id")


# ---------------------- SYNC TO GHL ---------------------- #

def push_screening_to_ghl(email: str, answers: list, legacy_code: str, prospect_id: str):
    """
    Updates the GHL contact record with Q1–Q6 answers + legacy code.
    Returns assigned user ID (coach) for routing.
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
            return None

        ghl_id = contact.get("id")

        assigned = (
            contact.get("assignedUserId")
            or contact.get("userId")
            or contact.get("assignedTo")
        )

        update_payload = {
            "tags": ["legacy screening submitted"],
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

        requests.put(
            f"{GHL_BASE_URL}/contacts/{ghl_id}",
            headers=headers,
            json=update_payload,
        )

        # Store Airtable Prospect ID inside GHL
        try:
            requests.put(
                f"{GHL_BASE_URL}/contacts/{ghl_id}",
                headers=headers,
                json={"customField": {"atrid": prospect_id}},
            )
        except:
            pass

        return assigned

    except Exception as e:
        print("GHL Screening Sync Error:", e)
        return None


# ---------------------- ROUTE: /submit ---------------------- #

@app.route("/submit", methods=["POST"])
def submit():
    try:
        data = request.json or {}
        email = (data.get("email") or "").strip()
        answers = data.get("answers") or []

        if not email:
            return jsonify({"error": "Missing email"}), 400

        # Guarantee 6 answers (prevent backend failure)
        while len(answers) < 6:
            answers.append("No response")

        # Create or find Prospect record
        legacy_code, prospect_id = get_or_create_prospect(email)

        # Save Q1–Q6 in Airtable
        save_screening_to_airtable(legacy_code, prospect_id, email, answers)

        # Sync into GHL
        assigned_user_id = push_screening_to_ghl(email, answers, legacy_code, prospect_id)

        # Build redirect URL to NextStep
        if assigned_user_id:
            redirect_url = f"{NEXTSTEP_URL}?uid={assigned_user_id}"
        else:
            redirect_url = NEXTSTEP_URL

        return jsonify({"redirect_url": redirect_url})

    except Exception as e:
        print("Submit Error:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
