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
USERS_TABLE = os.getenv("AIRTABLE_USERS_TABLE") or "Users"  # Add Users table

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
    """
    Look up the GHL User ID in the Users table and return their Legacy Code.
    """
    try:
        # Search Users table for the GHL User ID
        formula = f"{{GHL User ID}} = '{ghl_user_id}'"
        search_url = _url(USERS_TABLE, params={"filterByFormula": formula, "maxRecords": 1})
        
        r = requests.get(search_url, headers=_h())
        r.raise_for_status()
        data = r.json()
        
        if data.get("records"):
            user_record = data["records"][0]
            # Get the Legacy Code from the user record
            operator_legacy_code = user_record.get("fields", {}).get("Legacy Code")
            if operator_legacy_code:
                print(f"Found operator Legacy Code: {operator_legacy_code} for GHL User ID: {ghl_user_id}")
                return operator_legacy_code
            else:
                print(f"User found but no Legacy Code for GHL User ID: {ghl_user_id}")
        else:
            print(f"No user found with GHL User ID: {ghl_user_id}")
            
    except Exception as e:
        print(f"Error looking up operator Legacy Code: {e}")
    
    return None


# ---------------------- UPDATED: UPDATE PROSPECT WITH OPERATOR INFO ---------------------- #
def update_prospect_with_operator_info(prospect_id: str, ghl_user_id: str):
    """
    Update the Prospect record with GHL User ID and Assigned Op Legacy Code.
    """
    try:
        update_fields = {"GHL User ID": ghl_user_id}
        
        # Look up the operator's Legacy Code
        operator_legacy_code = get_operator_legacy_code(ghl_user_id)
        if operator_legacy_code:
            update_fields["Assigned Op Legacy Code"] = operator_legacy_code
        
        # Update the Prospect record
        r = requests.patch(
            _url(HQ_TABLE, prospect_id),
            headers=_h(),
            json={"fields": update_fields}
        )
        r.raise_for_status()
        print(f"Updated Prospect with GHL User ID: {ghl_user_id} and Op Legacy Code: {operator_legacy_code}")
        
    except Exception as e:
        print(f"Error updating prospect with operator info: {e}")


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
def save_screening_to_airtable(legacy_code: str, prospect_id: str, answers: list):
    """
    Writes NEW Q1–Q6 into the 'Survey Responses' table.
    Field names MUST match Airtable exactly.
    """
    fields = {
        "Legacy Code": legacy_code,
        "Prospects": [prospect_id],
        "Date Submitted": datetime.datetime.utcnow().isoformat(),

        # ✅ NEW Airtable fields (CONFIRMED EXACT)
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


# ---------------------- SYNC TO GHL (WITH CORRECT FIELD KEYS) ---------------------- #
def push_screening_to_ghl(email: str, answers: list, legacy_code: str, prospect_id: str):
    """
    Updates the GHL contact record with NEW Q1–Q6 answers + legacy code.
    Returns assigned user ID (coach) for routing.
    """
    try:
        headers = {
            "Authorization": f"Bearer {GHL_API_KEY}",
            "Content-Type": "application/json",
        }

        # Lookup contact by email
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
            print(f"No GHL contact found for email: {email}")
            return None

        ghl_id = contact.get("id")
        assigned = (
            contact.get("assignedUserId")
            or contact.get("userId")
            or contact.get("assignedTo")
        )

        # First, add the tag
        tag_response = requests.put(
            f"{GHL_BASE_URL}/contacts/{ghl_id}",
            headers=headers,
            json={"tags": ["legacy screening submitted"]}
        )
        print(f"Tag update status: {tag_response.status_code}")
        
        # ✅ CORRECTED: Use the ACTUAL GHL field keys from your screenshot
        # Note: The keys don't need "contact." prefix when sending via API
        field_updates = [
            {"q1_reason_for_business": answers[0]},      # Q1
            {"q2_lifework_starting_point": answers[1]},  # Q2
            {"q3_business_experience": answers[2]},      # Q3
            {"q4_startup_readiness": answers[3]},        # Q4
            {"q6_business_style_gem": answers[4]},       # Q5 (maps to q6_business_style_gem)
            # Note: Q6 "Ready to Follow 90-Day Plan?" doesn't have a GHL field
            {"legacy_code_id": legacy_code},
            {"atrid": prospect_id}
        ]
        
        # Update each custom field individually
        for field_update in field_updates:
            try:
                field_name = list(field_update.keys())[0]
                field_value = field_update[field_name]
                
                # Send each field update
                response = requests.put(
                    f"{GHL_BASE_URL}/contacts/{ghl_id}",
                    headers=headers,
                    json={"customField": field_update}
                )
                
                # Log the result
                if response.status_code == 200:
                    print(f"✅ Updated {field_name}: {field_value[:30] if len(str(field_value)) > 30 else field_value}")
                else:
                    print(f"❌ Failed to update {field_name}: Status {response.status_code}")
                    print(f"Response: {response.text}")
                    
                    # Try alternative format if first attempt fails
                    alt_response = requests.put(
                        f"{GHL_BASE_URL}/contacts/{ghl_id}",
                        headers=headers,
                        json=field_update  # Try without customField wrapper
                    )
                    if alt_response.status_code == 200:
                        print(f"✅ Updated {field_name} with alt format")
                        
            except Exception as e:
                print(f"Error updating field {field_update}: {e}")
                continue
        
        # ✅ Update Prospect with GHL User ID AND Operator's Legacy Code
        if assigned:
            update_prospect_with_operator_info(prospect_id, assigned)
        
        return assigned

    except Exception as e:
        print(f"GHL Screening Sync Error: {e}")
        return None


# ---------------------- ROUTE: Serve HTML ---------------------- #
@app.route("/")
def index():
    return render_template("chat.html")


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

        # Save NEW Q1–Q6 in Airtable
        save_screening_to_airtable(legacy_code, prospect_id, answers)

        # Sync into GHL (which will also update operator assignment)
        assigned_user_id = push_screening_to_ghl(email, answers, legacy_code, prospect_id)

        # Build redirect URL to NextStep
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
