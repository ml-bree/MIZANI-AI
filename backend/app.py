# app.py - Flask Backend with Supabase + Africa's Talking USSD + Sim Swap
# Dependencies: pip install flask supabase python-dotenv africastalking requests

import os
from flask import Flask, request, jsonify
from supabase import create_client, Client
from dotenv import load_dotenv
from africastalking import AfricasTalking  # For sim swap
import uuid
from datetime import datetime

load_dotenv()

app = Flask(__name__)

# Supabase client
supabase: Client = create_client(
    os.environ.get("SUPABASE_URL"),
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")  # Use service_role for writes
)

# Africa's Talking - Initialize for Sim Swap
at = AfricasTalking(
    username=os.environ.get("AFRICAS_TALKING_USERNAME"),
    api_key=os.environ.get("AFRICAS_TALKING_API_KEY")
)

# Demo Mode Toggle (for hackathon)
DEMO_MODE = True


@app.route('/api/analyze', methods=['POST'])
def analyze_candidate():
    """Core endpoint: Input candidate -> Output SPR analysis"""
    data = request.json
    candidate_name = data.get('candidate_name')
    constituency = data.get('constituency')

    # 1. Fetch candidate declared assets
    candidate = supabase.table('candidates').select(
        '*').eq('candidate_name', candidate_name).eq('constituency', constituency).execute()

    if not candidate.data:
        return jsonify({'error': 'Candidate not found'}), 404

    cand = candidate.data[0]
    declared_assets = cand['declared_assets']

    # 2. Aggregate expenditures (real or demo)
    expenditures = supabase.table('expenditures').select(
        '*').eq('candidate_id', cand['candidate_id']).execute()

    total_spend = sum(exp['amount'] for exp in expenditures.data)

    # 3. Simulate additional channels if demo
    if DEMO_MODE:
        # Mock Meta ads + crowdsourced
        total_spend += 2300000 + 1200000

    # 4. Calculate SPR
    denominator = declared_assets + declared_assets * 0.3
    spr = total_spend / denominator if denominator > 0 else 0

    # Alert level
    if spr > 1.5:
        alert = 'CRITICAL'
    elif spr > 1.2:
        alert = 'YELLOW'
    else:
        alert = 'GREEN'

    evidence = [
        {'type': 'meta_ads', 'value': 2300000, 'confidence': 0.89},
        {'type': 'crowdsourced', 'value': 1200000, 'confidence': 0.72}
    ]

    return jsonify({
        'declared_wealth': declared_assets,
        'estimated_spend': total_spend,
        'ratio': round(spr, 2),
        'alert_level': alert,
        'evidence': evidence
    })


@app.route('/api/ussd/callback', methods=['POST'])
def ussd_callback():
    """Africa's Talking USSD webhook - Report candidate expenditures"""
    session_id = request.values.get("sessionId")
    phone_number = request.values.get("phoneNumber")
    text = request.values.get("text", "")

    if text == "":
        response = "CON Enter candidate name and constituency (e.g. 'John Doe Starehe')\n"
    elif "*" in text:
        # Parse input: "John Doe Starehe"
        parts = text.split()
        if len(parts) >= 3:
            candidate_name = " ".join(parts[:-1])
            constituency = parts[-1]

            # Create demo candidate if not exists
            candidate = supabase.table('candidates').select(
                'candidate_id').eq('candidate_name', candidate_name).execute()
            if not candidate.data:
                new_cand = {
                    'candidate_name': candidate_name,
                    'constituency': constituency,
                    'political_party': 'Demo Party',
                    'declared_assets': 4500000
                }
                res = supabase.table('candidates').insert(new_cand).execute()
                candidate_id = res.data[0]['candidate_id']
            else:
                candidate_id = candidate.data[0]['candidate_id']

            # Store crowdsourced expenditure
            exp_data = {
                'candidate_id': candidate_id,
                'source_type': 'ussd_crowd',
                'amount': 1200000,  # Estimated from report
                'confidence_score': 0.72,
                'description_of_the_expenditure_spended': f'Crowdsourced report from {phone_number}',
                'location': constituency,
                'created_at': datetime.utcnow().isoformat()
            }
            supabase.table('expenditures').insert(exp_data).execute()

            response = f"END Report submitted for {candidate_name}! KSh 1.2M flagged."
        else:
            response = "END Invalid format. Try 'John Doe Starehe'"
    else:
        response = "END Invalid input."

    return response, 200, {'Content-Type': 'text/plain'}


@app.route('/api/sim-swap/<phone_number>', methods=['GET'])
def check_sim_swap(phone_number):
    """Check SIM swap status via Africa's Talking"""
    try:
        if DEMO_MODE:
            return jsonify({'phone_number': phone_number, 'swapped': False, 'confidence': 0.95})

        result = at.insights.check_sim_swap_state([phone_number])
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Health check


@app.route('/')
def health():
    return jsonify({'status': 'Mizani backend ready', 'demo_mode': DEMO_MODE})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

# -- Run in Supabase SQL Editor
CREATE TABLE candidates (
    candidate_id SERIAL PRIMARY KEY,
    candidate_name VARCHAR(255) NOT NULL,
    constituency VARCHAR(255) NOT NULL,
    political_party VARCHAR(255),
    declared_assets NUMERIC(12,2) DEFAULT 0
);

CREATE TABLE expenditures (
    expenditure_id SERIAL PRIMARY KEY,
    candidate_id INTEGER REFERENCES candidates(candidate_id),
    source_type VARCHAR(100),
    amount NUMERIC(12,2),
    confidence_score NUMERIC(3,2),
    description_of_the_expenditure_spended TEXT,
    location VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW()
);

-- Enable RLS (optional for demo)
ALTER TABLE candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE expenditures ENABLE ROW LEVEL SECURITY;

-- Sample data
INSERT INTO candidates (candidate_name, constituency, political_party, declared_assets) 
VALUES ('Candidate X', 'Starehe', 'Demo Party', 4500000);