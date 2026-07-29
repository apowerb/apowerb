import requests
import json

base_url = "http://localhost:8000"
agent_name = "agent65"  # python_architect_master
user_id = "34"
session_id = "test_session_manual_1"

# 1. Create Session
print(f"Creating session {session_id} for {agent_name}...")
create_url = f"{base_url}/apps/{agent_name}/users/{user_id}/sessions/{session_id}"
try:
    resp = requests.post(create_url, json={})
    print(f"Create Status: {resp.status_code}")
    print(f"Create Response: {resp.text}")
except Exception as e:
    print(f"Create Failed: {e}")

# 2. Run Agent
print(f"Running agent for session {session_id}...")
run_url = f"{base_url}/run_sse"
payload = {
    "app_name": agent_name,
    "user_id": user_id,
    "session_id": session_id,
    "new_message": {"parts": [{"text": "Hello"}]},
    "streaming": True
}
try:
    resp = requests.post(run_url, json=payload, stream=True)
    print(f"Run Status: {resp.status_code}")
    # Read first chunk to confirm
    for line in resp.iter_lines():
        if line:
            print(f"Run Chunk: {line}")
            break
except Exception as e:
    print(f"Run Failed: {e}")
