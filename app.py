# === ANGUS™ Survey Bot — Retrieves GHL User ID & User Email (with Enhanced Debugging) ===
# - Creates Prospect with unique Legacy Code
# - Searches for existing GHL contact by email
# - Retrieves the contact's assigned user ID AND user email from GHL
# - Stores both in Airtable Prospects table
# - Updates GHL contact with survey answers and tag
# - Adds tag: "rbr screening survey submitted"

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

BASE_ID = AIRTABLE_BASE_ID
HQ_TABLE = AIRTABLE_PROSPECTS_TABLE
RESPONSES_TABLE = AIRTABLE_RESPONSES_TABLE

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
# 1️⃣ Create Prospect Record
# ---------------------------------------------------------
def create_prospect_record(email):
    """
    Create a new Prospect with:
      - Prospect Email
      - Auto-generated Legacy Code (Legacy-X25-OP####)
    """
    # 1. Create prospect with email
    payload = {"fields": {"Prospect Email": email}}
    r = requests.post(_url(HQ_TABLE), headers=_h(), json=payload)
    r.raise_for_status()
    rec = r.json()
    rec_id = rec["id"]

    # 2. Get AutoNum
    auto = rec.get("fields", {}).get("AutoNum")
    if auto is None:
        r2 = requests.get(_url(HQ_TABLE, rec_id), headers=_h())
        r2.raise_for_status()
        auto = r2.json().get("fields", {}).get("AutoNum")

    if auto is None:
        raise RuntimeError("AutoNum missing from Prospects table.")

    # 3. Generate Prospect Legacy Code
    code_num = 1000 + int(auto)
    legacy_code = f"Legacy-X25-OP{code_num}"

    # 4. Update Prospect with Legacy Code
    requests.patch(
        _url(HQ_TABLE, rec_id), 
        headers=_h(), 
        json={"fields": {"Legacy Code": legacy_code}}
    )

    print(f"🧱 Created Prospect {rec_id} with Legacy Code {legacy_code}")
    return legacy_code, rec_id

# ---------------------------------------------------------
# 2️⃣ Push to GHL (Retrieve User ID & Email, Update Contact)
# ---------------------------------------------------------
def push_to_ghl(email, legacy_code, answers, record_id):
    """
    Find existing contact in GHL, retrieve their assigned user ID and user's email, 
    store both in Airtable, and update contact with survey data.
    """
    try:
        headers = {
            "Authorization": f"Bearer {GHL_API_KEY}",
            "Content-Type": "application/json",
        }
        
        # 1. Search for existing contact by email
        search_url = f"{GHL_BASE_URL}/contacts/lookup"
        search_params = {
            "email": email,
            "locationId": GHL_LOCATION_ID
        }
        
        search_response = requests.get(search_url, headers=headers, params=search_params)
        
        if search_response.status_code != 200:
            err = f"❌ Could not find contact with email {email}"
            print(err)
            requests.patch(
                _url(HQ_TABLE, record_id),
                headers=_h(),
                json={"fields": {"Sync Status": err}},
            )
            return
            
        contact_data = search_response.json()
        print(f"🔍 GHL Contact Data: {contact_data}")
        
        # Extract contact details
        if 'contacts' in contact_data and len(contact_data['contacts']) > 0:
            contact = contact_data['contacts'][0]
        elif 'contact' in contact_data:
            contact = contact_data['contact']
        else:
            contact = contact_data
            
        ghl_contact_id = contact.get('id')
        existing_user_id = (
            contact.get('assignedUserId') or 
            contact.get('userId') or 
            contact.get('assignedTo') or
            contact.get('assigned_user_id')
        )
        
        print(f"📌 Found GHL contact: {ghl_contact_id}")
        print(f"👤 Contact's assigned user ID: {existing_user_id}")
        
        # 2. If we have a user ID, get the user's details
        user_email = None
        if existing_user_id:
            try:
                # Try the users endpoint with location ID as a parameter
                user_url = f"{GHL_BASE_URL}/users/{existing_user_id}"
                print(f"🔍 Attempting to fetch user from: {user_url}")
                
                # Add location ID to headers or params
                user_params = {"locationId": GHL_LOCATION_ID}
                user_response = requests.get(user_url, headers=headers, params=user_params)
                
                print(f"📊 User API Status Code: {user_response.status_code}")
                print(f"📊 User API Response: {user_response.text[:500]}")  # First 500 chars
                
                if user_response.status_code == 200:
                    user_data = user_response.json()
                    print(f"🔍 Full User Data Structure: {user_data}")
                    
                    # Try multiple possible paths to find the email
                    user_email = (
                        user_data.get('email') or 
                        user_data.get('user', {}).get('email') or
                        user_data.get('userEmail') or
                        user_data.get('users', {}).get('email') or
                        user_data.get('data', {}).get('email')
                    )
                    
                    # If still no email, print all keys to see structure
                    if not user_email:
                        print(f"⚠️ Could not find email in user data. Available keys: {user_data.keys()}")
                    else:
                        print(f"📧 Found user email: {user_email}")
                        
                elif user_response.status_code == 404:
                    print(f"❌ User not found with ID: {existing_user_id}")
                elif user_response.status_code == 401:
                    print(f"❌ Unauthorized to access user data")
                else:
                    print(f"❌ User API error: {user_response.status_code} - {user_response.text}")
                    
            except Exception as e:
                print(f"❌ Exception fetching user details: {str(e)}")
        
        # 3. Update Airtable with both GHL User ID and Assigned Op Email
        airtable_updates = {}
        
        if existing_user_id:
            airtable_updates["GHL User ID"] = existing_user_id
            print(f"💾 Storing GHL User ID: {existing_user_id}")
            
        if user_email:
            airtable_updates["Assigned Op Email"] = user_email
            print(f"💾 Storing Assigned Op Email: {user_email}")
        else:
            print("⚠️ No email found for user, only storing User ID")
            
        if airtable_updates:
            requests.patch(
                _url(HQ_TABLE, record_id),
                headers=_h(),
                json={"fields": airtable_updates},
            )
        else:
            print("⚠️ No user information found for this contact in GHL")
        
        # 4. Update the GHL contact with survey answers and tag
        update_url = f"{GHL_BASE_URL}/contacts/{ghl_contact_id}"
        
        update_payload = {
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
        
        r = requests.put(update_url, headers=headers, json=update_payload)
        
        if r.status_code == 200:
            print("✅ Updated contact in GHL with survey data")
            requests.patch(
                _url(HQ_TABLE, record_id),
                headers=_h(),
                json={"fields": {"Sync Status": "✅ Synced to GHL"}},
            )
        else:
            err = f"❌ GHL Update Error {r.status_code}: {r.text}"
            print(err)
            requests.patch(
                _url(HQ_TABLE, record_id),
                headers=_h(),
                json={"fields": {"Sync Status": err}},
            )
            
    except Exception as e:
        err = f"❌ Exception during GHL sync: {str(e)}"
        print(err)
        requests.patch(
            _url(HQ_TABLE, record_id),
            headers=_h(),
            json={"fields": {"Sync Status": err}},
        )

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

        # Ensure always 6 answers
        while len(answers) < 6:
            answers.append("No response provided")

        # 1. Create Prospect with Legacy Code
        legacy_code, prospect_id = create_prospect_record(email)

        # 2. Save survey responses into Survey Responses table
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
            print(f"❌ Airtable error saving survey responses: {r3.status_code} {r3.text}")

        # 3. Background delay before syncing to GHL
        print("⏱ Waiting 60 seconds before GHL sync...")
        time.sleep(60)

        # 4. Final sync to GHL (will retrieve and store both User ID and Email)
        push_to_ghl(email, legacy_code, answers, prospect_id)

        return jsonify({"status": "ok", "legacy_code": legacy_code})

    except Exception as e:
        print(f"🔥 Error in /submit: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------
# 4️⃣ Debug Routes
# ---------------------------------------------------------
@app.route("/debug_contact/<email>")
def debug_contact(email):
    """Debug route to test GHL contact lookup"""
    try:
        headers = {
            "Authorization": f"Bearer {GHL_API_KEY}",
            "Content-Type": "application/json",
        }
        
        search_url = f"{GHL_BASE_URL}/contacts/lookup"
        search_params = {
            "email": email,
            "locationId": GHL_LOCATION_ID
        }
        
        response = requests.get(search_url, headers=headers, params=search_params)
        
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": f"Status {response.status_code}", "text": response.text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/debug_user/<user_id>")
def debug_user(user_id):
    """Debug route to test GHL user lookup"""
    try:
        headers = {
            "Authorization": f"Bearer {GHL_API_KEY}",
            "Content-Type": "application/json",
        }
        
        user_url = f"{GHL_BASE_URL}/users/{user_id}"
        user_params = {"locationId": GHL_LOCATION_ID}
        response = requests.get(user_url, headers=headers, params=user_params)
        
        print(f"Debug User URL: {user_url}")
        print(f"Debug User Status: {response.status_code}")
        print(f"Debug User Response: {response.text[:1000]}")
        
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": f"Status {response.status_code}", "text": response.text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

    if not GHL_API_KEY or not GHL_LOCATION_ID:
        print("❌ Missing GHL environment variables.")
        exit(1)

    print("🚀 Starting Angus Survey Bot (User ID + Email Retrieval with Debugging)")
    app.run(debug=True, host="0.0.0.0", port=5000)
