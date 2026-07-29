import requests
try:
    resp = requests.post("http://localhost:8000/run_sse", json={
        "app_name": "test",
        "user_id": "test",
        "session_id": "test",
        "new_message": {"parts": [{"text": "hi"}]}
    })
    print(f"Status Code: {resp.status_code}")
    print(f"Response: {resp.text}")
except Exception as e:
    print(f"Error: {e}")
