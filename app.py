from flask import Flask, request, jsonify, render_template
import requests
import os
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ---------------------- CONFIG ---------------------- #
# LegacyOS endpoint (replaces Airtable)
LEGACYOS_URL = os.getenv("LEGACYOS_URL") or "https://legacyos-5buihoc3d-rick-wendts-projects.vercel.app/api/submit-survey"

# GoHighLevel
GHL_API_KEY = os.getenv("GHL_API_KEY")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID")
GHL_BASE_URL = "https://rest.gohighlevel.com/v1"

NEXTSTEP_URL = os.getenv("NEXTSTEP_URL") or "https://poweredbylegacycode.com/nextstep"


# ---------------------- LEGACYOS SUBMISSION ---------------------- #
def submit_to_legacyos(email: str, answers: list):
    """
    Submit survey to LegacyOS and get back the Legacy Code.
    Replaces all Airtable calls.
    """
    try:
        payload = {
            "email": email,
            "q1": answers[0] if len(answers) > 0 else "No response",
            "q2": answers[1] if len(answers) > 1 else "No response",
            "q3": answers[2] if len(answers) > 2 else "No response",
            "q4": answers[3] if len(answers) > 3 else "No response",
            "q5": answers[4] if len(answers) > 4 else "No response",
            "q6": answers[5] if len(answers) > 5 else "No response",
        }
        
        response = requests.post(LEGACYOS_URL, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if data.get("success"):
            return data.get("legacyCode"), None
        else:
            # Email might already exist
            return data.get("legacyCode"), data.get("error")
            
    except Exception as e:
        print(f"❌ LegacyOS submission error: {e}")
        return None, str(e)


# ---------------------- GHL SYNC — SNAPSHOT SURVEY ---------------------- #
def push_snapshot_survey_to_ghl(email: str, answers: list, legacy_code: str):
    """
    Snapshot Survey Q1–Q6 to GoHighLevel.
    """
    try:
        headers = {
            "Authorization": f"Bearer {GHL_API_KEY}",
            "Content-Type": "application/json",
        }

        # Look up contact by email
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

        # Add tag
        tag_response = requests.put(
            f"{GHL_BASE_URL}/contacts/{ghl_id}",
            headers=headers,
            json={"tags": ["snapshot survey submitted"]}
        )
        print(f"Tag update status: {tag_response.status_code}")

        # ---------- BATCH: Q1–Q6 + Legacy Code ----------
        custom_fields = {
            "q1_reason_for_business": str(answers[0]) if len(answers) > 0 else "",
            "q2_lifework_starting_point": str(answers[1]) if len(answers) > 1 else "",
            "q3_business_experience": str(answers[2]) if len(answers) > 2 else "",
            "q4_startup_readiness": str(answers[3]) if len(answers) > 3 else "",
            "q5_work_style": str(answers[4]) if len(answers) > 4 else "",
            "q6_business_style_gem": str(answers[5]) if len(answers) > 5 else "",
            "legacy_code_id": legacy_code,
        }

        print("------ SENDING TO GHL ------")
        for key, val in custom_fields.items():
            print(f"Field: {key} | Value: {str(val)[:60]}")
        print("----------------------------")

        batch_response = requests.put(
            f"{GHL_BASE_URL}/contacts/{ghl_id}",
            headers=headers,
            json={"customField": custom_fields}
        )

        print(f"📊 GHL batch update status: {batch_response.status_code}")

        if batch_response.status_code != 200:
            print("❌ GHL batch failed:")
            print(batch_response.text[:500])
        else:
            print("✅ GHL batch SUCCESS")

        return assigned

    except Exception as e:
        print(f"❌ GHL Sync Error: {e}")
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

        # Pad answers to 6 if needed
        while len(answers) < 6:
            answers.append("No response")

        # Submit to LegacyOS (replaces Airtable)
        legacy_code, error = submit_to_legacyos(email, answers)
        
        if not legacy_code:
            print(f"❌ Failed to get Legacy Code: {error}")
            return jsonify({"error": "Failed to create prospect"}), 500

        print(f"✅ Got Legacy Code from LegacyOS: {legacy_code}")

        # Sync to GoHighLevel
        assigned_user_id = push_snapshot_survey_to_ghl(email, answers, legacy_code)

        # Build redirect URL
        if assigned_user_id:
            redirect_url = f"{NEXTSTEP_URL}?uid={assigned_user_id}"
        else:
            redirect_url = NEXTSTEP_URL

        return jsonify({
            "redirect_url": redirect_url,
            "legacy_code": legacy_code
        })

    except Exception as e:
        print(f"Submit Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
