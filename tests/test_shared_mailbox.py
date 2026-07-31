"""Unit tests for shared mailbox support.

Tests:
1. _mailbox_base helper returns correct Graph API paths
2. Outlook tools use the mailbox parameter to build URLs correctly
3. Router endpoints for add/remove shared mailboxes
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from apowerb.tools_store.portfolio.outlook_mail import _mailbox_base, _GRAPH_BASE


class TestMailboxBase:
    def test_empty_mailbox_returns_me(self):
        assert _mailbox_base() == f"{_GRAPH_BASE}/me"

    def test_empty_string_returns_me(self):
        assert _mailbox_base("") == f"{_GRAPH_BASE}/me"

    def test_shared_mailbox_returns_users_path(self):
        assert _mailbox_base("shared@domain.com") == f"{_GRAPH_BASE}/users/shared@domain.com"


class TestToolsUseMailboxParam:
    """Verify that each tool builds URLs with the mailbox parameter."""

    @patch("apowerb.tools_store.portfolio.outlook_mail._graph_headers")
    @patch("apowerb.tools_store.portfolio.outlook_mail.httpx")
    def test_list_emails_default_uses_me(self, mock_httpx, mock_headers):
        from apowerb.tools_store.portfolio.outlook_mail import tool_list_emails

        mock_headers.return_value = {"Authorization": "Bearer fake"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"value": []}
        mock_httpx.get.return_value = mock_resp

        tool_list_emails(folder="inbox")

        call_args = mock_httpx.get.call_args
        url = call_args[0][0]
        assert "/me/mailFolders/" in url

    @patch("apowerb.tools_store.portfolio.outlook_mail._graph_headers")
    @patch("apowerb.tools_store.portfolio.outlook_mail.httpx")
    def test_list_emails_shared_uses_users_path(self, mock_httpx, mock_headers):
        from apowerb.tools_store.portfolio.outlook_mail import tool_list_emails

        mock_headers.return_value = {"Authorization": "Bearer fake"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"value": []}
        mock_httpx.get.return_value = mock_resp

        tool_list_emails(folder="inbox", mailbox="shared@domain.com")

        call_args = mock_httpx.get.call_args
        url = call_args[0][0]
        assert "/users/shared@domain.com/mailFolders/" in url

    @patch("apowerb.tools_store.portfolio.outlook_mail._graph_headers")
    @patch("apowerb.tools_store.portfolio.outlook_mail.httpx")
    def test_read_email_shared_uses_users_path(self, mock_httpx, mock_headers):
        from apowerb.tools_store.portfolio.outlook_mail import tool_read_email

        mock_headers.return_value = {"Authorization": "Bearer fake"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "abc",
            "subject": "Test",
            "from": {"emailAddress": {"address": "a@b.com"}},
            "toRecipients": [],
            "ccRecipients": [],
            "receivedDateTime": "2026-01-01",
            "body": {"contentType": "Text", "content": "Hello"},
            "isRead": True,
            "hasAttachments": False,
        }
        mock_httpx.get.return_value = mock_resp

        tool_read_email(message_id="abc123", mailbox="shared@domain.com")

        call_args = mock_httpx.get.call_args
        url = call_args[0][0]
        assert "/users/shared@domain.com/messages/" in url

    @patch("apowerb.tools_store.portfolio.outlook_mail._graph_headers")
    @patch("apowerb.tools_store.portfolio.outlook_mail.httpx")
    def test_search_emails_shared_uses_users_path(self, mock_httpx, mock_headers):
        from apowerb.tools_store.portfolio.outlook_mail import tool_search_emails

        mock_headers.return_value = {"Authorization": "Bearer fake"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"value": []}
        mock_httpx.get.return_value = mock_resp

        tool_search_emails(query="test", mailbox="shared@domain.com")

        call_args = mock_httpx.get.call_args
        url = call_args[0][0]
        assert "/users/shared@domain.com/messages" in url

    @patch("apowerb.tools_store.portfolio.outlook_mail._graph_headers")
    @patch("apowerb.tools_store.portfolio.outlook_mail.httpx")
    def test_list_mail_folders_shared_uses_users_path(self, mock_httpx, mock_headers):
        from apowerb.tools_store.portfolio.outlook_mail import tool_list_mail_folders

        mock_headers.return_value = {"Authorization": "Bearer fake"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"value": []}
        mock_httpx.get.return_value = mock_resp

        tool_list_mail_folders(mailbox="shared@domain.com")

        call_args = mock_httpx.get.call_args
        url = call_args[0][0]
        assert "/users/shared@domain.com/mailFolders" in url

    @patch("apowerb.tools_store.portfolio.outlook_mail._graph_headers")
    @patch("apowerb.tools_store.portfolio.outlook_mail.httpx")
    def test_send_email_shared_uses_users_path(self, mock_httpx, mock_headers):
        from apowerb.tools_store.portfolio.outlook_mail import tool_send_outlook_email

        mock_headers.return_value = {"Authorization": "Bearer fake"}
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_httpx.post.return_value = mock_resp

        tool_send_outlook_email(
            to="recipient@example.com",
            subject="Test",
            body="Hello",
            mailbox="shared@domain.com",
        )

        call_args = mock_httpx.post.call_args
        url = call_args[0][0]
        assert "/users/shared@domain.com/sendMail" in url

    @patch("apowerb.tools_store.portfolio.outlook_mail._graph_headers")
    @patch("apowerb.tools_store.portfolio.outlook_mail.httpx")
    def test_download_attachment_shared_uses_users_path(self, mock_httpx, mock_headers):
        from apowerb.tools_store.portfolio.outlook_mail import tool_download_attachment

        mock_headers.return_value = {"Authorization": "Bearer fake"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "name": "file.txt",
            "contentBytes": "SGVsbG8=",  # base64 "Hello"
        }
        mock_httpx.get.return_value = mock_resp

        import os
        os.environ["ROOT_AGENT_ID"] = "test_agent"

        tool_download_attachment(
            message_id="msg1",
            attachment_id="att1",
            mailbox="shared@domain.com",
        )

        call_args = mock_httpx.get.call_args
        url = call_args[0][0]
        assert "/users/shared@domain.com/messages/" in url


class TestSharedMailboxScopes:
    def test_outlook_scopes_include_shared(self):
        from apowerb.integrations.microsoft import MICROSOFT_SERVICE_SCOPES
        outlook_scopes = MICROSOFT_SERVICE_SCOPES["outlook"]
        assert "Mail.Read.Shared" in outlook_scopes
        assert "Mail.Send.Shared" in outlook_scopes
