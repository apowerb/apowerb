#!/usr/bin/env python3
"""
End-to-end test for BI & Reporting API.
Tests the full CRUD flow: dashboards, charts, components, stats.
"""

import sys
import datetime
import json

import os

import requests
from jose import jwt

# ---------------------------------------------------------------------------
# Configuration — read from environment, never hardcode secrets
# ---------------------------------------------------------------------------
BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:8000")
API = f"{BASE_URL}/api/v1"

ENCRYPT_KEY = os.environ["ENCRYPT_KEY"]
ALGORITHM = "HS256"

USER_EMAIL = os.environ["E2E_USER_EMAIL"]


def make_token() -> str:
    """Create a valid JWT access token."""
    payload = {
        "sub": USER_EMAIL,
        "exp": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=2),
        "iat": datetime.datetime.now(datetime.timezone.utc),
    }
    return jwt.encode(payload, ENCRYPT_KEY, algorithm=ALGORITHM)


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------
TOKEN = make_token()
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

passed = 0
failed = 0
results = []


def test(label: str, response: requests.Response, expected_status: int) -> dict | None:
    """Check response status; print PASS/FAIL; return parsed JSON or None."""
    global passed, failed
    ok = response.status_code == expected_status
    tag = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1

    print(f"[{tag}] {label}")
    print(f"       Status: {response.status_code} (expected {expected_status})")
    if not ok:
        try:
            print(f"       Body:   {response.text[:500]}")
        except Exception:
            print(f"       Body:   <unreadable>")
    results.append((label, response.status_code, expected_status, ok))

    if response.status_code == 204 or not response.text:
        return None
    try:
        return response.json()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------

def main():
    global passed, failed

    print("=" * 70)
    print("BI & Reporting API — End-to-End Test")
    print("=" * 70)
    print(f"Server : {BASE_URL}")
    print(f"Token  : {TOKEN[:40]}...")
    print()

    # 0. Health check
    r = requests.get(f"{BASE_URL}/health")
    data = test("GET /health", r, 200)
    if not data or data.get("status") != "ok":
        print("Server is not healthy — aborting.")
        sys.exit(1)

    # 1. GET /api/v1/bi/stats — baseline
    r = requests.get(f"{API}/bi/stats", headers=HEADERS)
    stats_before = test("GET /api/v1/bi/stats (baseline)", r, 200)
    if stats_before:
        print(f"       Data:   dashboard_count={stats_before.get('dashboard_count')}, "
              f"chart_count={stats_before.get('chart_count')}")

    # 2. POST /api/v1/dashboards — create
    r = requests.post(
        f"{API}/dashboards",
        headers=HEADERS,
        json={"title": "Test Dashboard E2E", "description": "Created by e2e test"},
    )
    dashboard = test("POST /api/v1/dashboards (create)", r, 201)
    dashboard_id = dashboard["id"] if dashboard else None
    if dashboard:
        print(f"       Data:   id={dashboard_id}")

    # 3. GET /api/v1/dashboards — list
    r = requests.get(f"{API}/dashboards", headers=HEADERS)
    dash_list = test("GET /api/v1/dashboards (list)", r, 200)
    if dash_list and dashboard_id:
        found = any(d["id"] == dashboard_id for d in dash_list.get("items", []))
        if found:
            print("       Check:  Created dashboard found in list -> OK")
        else:
            print("       Check:  Created dashboard NOT in list -> PROBLEM")

    # 4. GET /api/v1/dashboards/{id}
    if dashboard_id:
        r = requests.get(f"{API}/dashboards/{dashboard_id}", headers=HEADERS)
        dash_detail = test(f"GET /api/v1/dashboards/{dashboard_id}", r, 200)
        if dash_detail:
            print(f"       Data:   title={dash_detail.get('title')}, "
                  f"components={len(dash_detail.get('components', []))}")

    # 5. POST /api/v1/charts — create
    r = requests.post(
        f"{API}/charts",
        headers=HEADERS,
        json={
            "name": "Test Chart E2E",
            "title": "Test Chart E2E",
            "chart_type": "bar",
            "organization_id": "default",
            "source": {"query": "agent_runs_hourly"},
        },
    )
    chart = test("POST /api/v1/charts (create)", r, 201)
    chart_id = chart["id"] if chart else None
    if chart:
        print(f"       Data:   id={chart_id}")

    # 6. GET /api/v1/charts/{id}/data — fetch chart data
    if chart_id:
        r = requests.get(f"{API}/charts/{chart_id}/data", headers=HEADERS)
        chart_data = test(f"GET /api/v1/charts/{chart_id}/data", r, 200)
        if chart_data:
            rows = chart_data.get("rows", [])
            print(f"       Data:   rows_returned={len(rows)}, "
                  f"total={chart_data.get('total', '?')}")

    # 7. POST /api/v1/dashboards/{id}/components — add chart component
    component_id = None
    if dashboard_id and chart_id:
        r = requests.post(
            f"{API}/dashboards/{dashboard_id}/components",
            headers=HEADERS,
            json={
                "component": {
                    "component_type": "chart",
                    "position": {"col": 0, "row": 0, "width": 6, "height": 4},
                    "chart": {"chart_id": chart_id},
                }
            },
        )
        dash_with_comp = test(
            f"POST /api/v1/dashboards/{dashboard_id}/components (add chart)", r, 201
        )
        if dash_with_comp:
            comps = dash_with_comp.get("components", [])
            print(f"       Data:   component_count={len(comps)}")
            if comps:
                component_id = comps[-1]["id"]
                print(f"       Data:   component_id={component_id}")

    # 8. GET /api/v1/dashboards/{id} — verify component attached
    if dashboard_id:
        r = requests.get(f"{API}/dashboards/{dashboard_id}", headers=HEADERS)
        dash_verify = test(
            f"GET /api/v1/dashboards/{dashboard_id} (verify component)", r, 200
        )
        if dash_verify:
            comps = dash_verify.get("components", [])
            has_chart = any(
                c.get("chart", {}).get("chart_id") == chart_id for c in comps if c.get("chart")
            )
            print(f"       Check:  components={len(comps)}, "
                  f"has_our_chart={'YES' if has_chart else 'NO'}")

    # 9. DELETE /api/v1/dashboards/{id}/components/{component_id}
    if dashboard_id and component_id:
        r = requests.delete(
            f"{API}/dashboards/{dashboard_id}/components/{component_id}",
            headers=HEADERS,
        )
        del_comp = test(
            f"DELETE /api/v1/dashboards/{dashboard_id}/components/{component_id}",
            r,
            200,
        )
        if del_comp:
            comps = del_comp.get("components", [])
            print(f"       Data:   remaining_components={len(comps)}")

    # 10. DELETE /api/v1/charts/{id}
    if chart_id:
        r = requests.delete(f"{API}/charts/{chart_id}", headers=HEADERS)
        test(f"DELETE /api/v1/charts/{chart_id}", r, 204)

    # 11. DELETE /api/v1/dashboards/{id}
    if dashboard_id:
        r = requests.delete(f"{API}/dashboards/{dashboard_id}", headers=HEADERS)
        test(f"DELETE /api/v1/dashboards/{dashboard_id}", r, 204)

    # 12. GET /api/v1/bi/stats — verify counts restored
    r = requests.get(f"{API}/bi/stats", headers=HEADERS)
    stats_after = test("GET /api/v1/bi/stats (after cleanup)", r, 200)
    if stats_after and stats_before:
        dash_ok = stats_after.get("dashboard_count") == stats_before.get("dashboard_count")
        chart_ok = stats_after.get("chart_count") == stats_before.get("chart_count")
        print(f"       Data:   dashboard_count={stats_after.get('dashboard_count')} "
              f"(was {stats_before.get('dashboard_count')}) -> {'OK' if dash_ok else 'MISMATCH'}")
        print(f"       Data:   chart_count={stats_after.get('chart_count')} "
              f"(was {stats_before.get('chart_count')}) -> {'OK' if chart_ok else 'MISMATCH'}")

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    print()
    print("=" * 70)
    print(f"RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
    print("=" * 70)
    for label, got, expected, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}  [{got}/{expected}]")
    print()

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
