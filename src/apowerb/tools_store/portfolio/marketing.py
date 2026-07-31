"""HubSpot marketing and sales tools."""

from typing import Dict, Any, Optional, List
import requests
import os


def tool_hubspot_get_sales_leads(
    limit: int = 100,
    after_date: Optional[str] = None,
    properties: Optional[List[str]] = None,
    api_key: Optional[str] = None,
    filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Retrieve sales leads from HubSpot.

    Args:
            limit: Maximum number of leads to return (1-100, default 100)
            after_date: ISO 8601 date string to filter leads modified after this date
            properties: List of HubSpot property names to retrieve (e.g., ['firstname', 'email', 'lifecyclestage'])
            api_key: HubSpot API key (defaults to HUBSPOT_API_KEY env var)
            filters: Optional filter criteria as dict (e.g., {'lifecyclestage': 'subscriber'})

    Returns:
            {status: 'success', leads: [...], count: N, total: N} or error dict
    """
    # Get API key from env if not provided
    if not api_key:
        api_key = os.getenv("HUBSPOT_API_KEY")

    if not api_key:
        return {
            "status": "error",
            "error_message": "HubSpot API key required (set HUBSPOT_API_KEY env var)",
        }

    if limit < 1 or limit > 100:
        return {"status": "error", "error_message": "limit must be between 1 and 100"}

    # Default properties if none specified
    if not properties:
        properties = [
            "firstname",
            "lastname",
            "email",
            "phone",
            "company",
            "lifecyclestage",
            "hs_lead_status",
            "createdate",
            "hs_analytics_num_visits",
        ]

    url = "https://api.hubapi.com/crm/v3/objects/contacts"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # Build query params
    params = {"limit": limit, "properties": properties, "archived": False}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        # Extract leads from response
        leads = []
        for contact in data.get("results", []):
            lead_data = contact.get("properties", {})
            leads.append(
                {
                    "id": contact.get("id"),
                    "firstname": lead_data.get("firstname", ""),
                    "lastname": lead_data.get("lastname", ""),
                    "email": lead_data.get("email", ""),
                    "phone": lead_data.get("phone", ""),
                    "company": lead_data.get("company", ""),
                    "lifecyclestage": lead_data.get("lifecyclestage", ""),
                    "lead_status": lead_data.get("hs_lead_status", ""),
                    "created_date": lead_data.get("createdate", ""),
                    "num_visits": lead_data.get("hs_analytics_num_visits", 0),
                    "raw": lead_data,
                }
            )

        return {
            "status": "success",
            "leads": leads,
            "count": len(leads),
            "total": data.get("paging", {}).get("total", len(leads)),
        }

    except requests.exceptions.RequestException as e:
        return {"status": "error", "error_message": f"HubSpot API error: {str(e)}"}


def tool_marketing():
    """Placeholder function for marketing tool."""
    pass
