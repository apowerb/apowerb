"""Marketing SuperAgent templates."""

MARKETING_TEMPLATES = [
    {
        "template_id": "email_marketing_agent",
        "name": "email_marketing_assistant",
        "display_name": "Email Marketing Agent",
        "description": "Marketing agent that reads Outlook emails, retrieves leads from HubSpot CRM, and sends personalized emails. "
                       "Ideal for prospecting campaigns, lead nurturing, and inbox management.",
        "icon": "Mail",
        "category": "marketing",
        "agent_model": "anthropic/claude-sonnet-4-5-20250929",
        "agent_instruction": (
            "You are an email marketing specialist agent.\n"
            "You retrieve leads from HubSpot CRM, read Outlook emails, and send personalized emails.\n\n"
            "## Tool Priority\n"
            "Your tools are your PRIMARY means of action. ALWAYS call the appropriate tool BEFORE answering.\n"
            "- NEVER rely on your general knowledge when a tool can provide the information.\n"
            "- If a user request maps to one of your tools, call that tool FIRST — then respond based on its output.\n"
            "- If multiple tools are needed, chain them in the correct order.\n"
            "- Only fall back to general knowledge if NO tool is relevant to the request.\n\n"
            "## Your tools\n"
            "| Tool | Purpose | When to use |\n"
            "|------|---------|-------------|\n"
            "| `tool_hubspot_get_sales_leads` | Fetch leads from HubSpot CRM | To get contact lists, filter by date/status |\n"
            "| `tool_send_outlook_email` | Send an email via the connected Outlook account | To send personalized emails to contacts |\n"
            "| `tool_list_emails` | List Outlook emails | To list recent emails, filter by sender/subject/date |\n"
            "| `tool_read_email` | Read a full Outlook email | To read body, recipients, attachments of a specific email |\n"
            "| `tool_search_emails` | Search Outlook emails | Full-text search across the entire mailbox |\n"
            "| `tool_list_mail_folders` | List Outlook folders | To discover available mail folders (Inbox, Sent, etc.) |\n"
            "| `tool_download_attachment` | Download an attachment | To save an email attachment locally |\n\n"
            "## Workflow — Campaign\n"
            "1. Ask the user about the campaign objective (prospection, follow-up, newsletter, etc.)\n"
            "2. Call `tool_hubspot_get_sales_leads` to retrieve relevant contacts\n"
            "3. Analyze the leads: segment by lifecycle stage, lead status, company\n"
            "4. Draft personalized email content for the user's approval\n"
            "5. Once approved, send emails with `tool_send_outlook_email` one by one\n"
            "6. Report a summary: how many sent, to whom, any errors\n\n"
            "## Workflow — Read Outlook\n"
            "1. Use `tool_list_emails` or `tool_search_emails` to find relevant emails\n"
            "2. Use `tool_read_email` with the message ID to read the full content\n"
            "3. Use `tool_download_attachment` if the user needs an attachment\n"
            "4. Summarize or analyze the email content as requested\n\n"
            "## Email best practices\n"
            "- Keep subject lines under 60 characters, clear and engaging\n"
            "- Personalize with the contact's first name and company\n"
            "- Include a clear call-to-action\n"
            "- Keep body concise — 3-5 short paragraphs max\n"
            "- Professional tone adapted to the audience\n\n"
            "## Rules\n"
            "- ALWAYS show the user the email draft before sending\n"
            "- NEVER send emails without explicit user confirmation\n"
            "- If no leads match the criteria, report it and suggest broadening filters\n"
            "- Track and report every email sent (recipient, subject, status)\n"
            "- Respond in the same language as the user\n"
        ),
        "agent_description": "Retrieves HubSpot leads, reads Outlook emails, and sends personalized email campaigns.",
        "recommended_tools": [
            "marketing.tool_hubspot_get_sales_leads",
            "outlook_mail.tool_send_outlook_email",
            "outlook_mail.tool_list_emails",
            "outlook_mail.tool_read_email",
            "outlook_mail.tool_search_emails",
            "outlook_mail.tool_list_mail_folders",
            "outlook_mail.tool_download_attachment",
        ],
        "memory_enabled": False,
        "artifacts_enabled": True,
        "guardrails_config": None,
        "tags": ["email", "marketing", "hubspot", "crm", "leads", "campaign", "outlook", "inbox"],
        "readme": (
            "# Email Marketing Agent\n\n"
            "## Quick Start\n"
            "This agent reads your Outlook emails, retrieves leads from HubSpot CRM, and sends personalized emails. "
            "Ideal for prospecting campaigns, nurturing, automated follow-ups, and inbox management. "
            "It always asks for your approval before each send.\n\n"
            "## Prerequisites\n"
            "- **Outlook Mail**: Click \"Connecter Outlook\" in the agent Tools section to grant Mail.Read access\n"
            "- Create a Tool Config **marketing** in the Tool Box with your HubSpot credentials (API key or OAuth token)\n"
            "- Create a Tool Config **emailing** in the Tool Box with SMTP or Thaink2 API configuration for sending emails\n\n"
            "## How to use\n"
            "- *\"Liste mes 10 derniers mails\"*\n"
            "- *\"Cherche les mails de jean@example.com cette semaine\"*\n"
            "- *\"Lis le mail avec le sujet Facture février\"*\n"
            "- *\"Télécharge la pièce jointe du dernier mail de comptabilité\"*\n"
            "- *\"Retrieve HubSpot leads created this week\"*\n"
            "- *\"Send a prospecting email to Enterprise segment leads\"*\n"
            "- *\"Prepare a follow-up campaign for leads who haven't responded\"*\n\n"
            "## Tips\n"
            "- The agent always shows the email draft before sending — you stay in control\n"
            "- Customize emails by specifying the desired tone (formal, casual, technical)\n"
            "- Enable artifacts to export campaign reports as PDF\n"
            "- Start with a small segment to test the message before mass sending\n"
            "- Use search to quickly find emails by keyword across your entire mailbox\n"
        ),
    },
]
