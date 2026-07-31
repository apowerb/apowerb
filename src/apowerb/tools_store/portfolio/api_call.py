import requests
from typing import Dict, Any


def tool_api_call(api_url: str, payload: dict, token: str) -> dict:
    """Placeholder function for API call tool."""
    return {"status": "success", "data": {"api_url": api_url, "payload": payload}}


def tool_thaink2_forecast(api_url: str, payload: dict, token: str) -> Dict[str, Any]:
    """
    Call Thaink² Forecast API.

    Parameters
    ----------
    api_url : str
        Forecast API endpoint
    payload : dict
        Forecast request payload
    token : str
        Authentication token (Bearer)

    Returns
    -------
    Dict[str, Any]
        API response JSON

    Raises
    ------
    RuntimeError
        If the API call fails
    """

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        response = requests.post(api_url, json=payload, headers=headers, timeout=30)

        # Raise HTTPError for 4xx / 5xx
        response.raise_for_status()

        return response.json()

    except requests.exceptions.Timeout:
        return {"success": False, "error": "Forecast API request timed out"}

    except requests.exceptions.HTTPError:
        try:
            error_detail = response.json()
        except Exception:
            error_detail = response.text
        return {"success": False, "error": f"Forecast API error {response.status_code}: {error_detail}"}

    except requests.exceptions.RequestException as e:
        return {"success": False, "error": f"Forecast API request failed: {str(e)}"}
