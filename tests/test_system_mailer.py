"""Unit tests for the headless system mailer (notifications shared mailbox)."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from apowerb.helpers import system_mailer


def _resp(status: int, text: str = ""):
    r = MagicMock()
    r.status_code = status
    r.text = text
    return r


class TestPostSendMail:
    @patch("apowerb.helpers.system_mailer.httpx")
    def test_posts_to_shared_mailbox_path(self, mock_httpx):
        mock_httpx.post.return_value = _resp(202)
        ok = system_mailer._post_send_mail(
            token="tok", shared="notifications@thaink2.com",
            to="x@y.com", subject="S", html="<p>hi</p>",
        )
        assert ok is True
        url = mock_httpx.post.call_args[0][0]
        assert url == f"{system_mailer._GRAPH_BASE}/users/notifications@thaink2.com/sendMail"
        payload = mock_httpx.post.call_args.kwargs["json"]
        assert payload["message"]["toRecipients"][0]["emailAddress"]["address"] == "x@y.com"
        assert payload["saveToSentItems"] is True

    @patch("apowerb.helpers.system_mailer.httpx")
    def test_non_202_returns_false(self, mock_httpx):
        mock_httpx.post.return_value = _resp(403, "Forbidden")
        assert system_mailer._post_send_mail(
            token="t", shared="s@x.com", to="a@b.com", subject="S", html="h"
        ) is False


class TestSendSystemEmail:
    @pytest.mark.asyncio
    @patch("apowerb.helpers.system_mailer._post_send_mail", return_value=True)
    @patch("apowerb.helpers.system_mailer._get_owner_token", new_callable=AsyncMock)
    async def test_success_returns_true_no_fallback(self, mock_token, mock_post):
        mock_token.return_value = "valid-token"
        with patch("apowerb.helpers.system_mailer.email_sender.send_email",
                   new_callable=AsyncMock) as mock_fallback:
            ok = await system_mailer.send_system_email(
                to="u@v.com", subject="Sub", html="<b>x</b>"
            )
        assert ok is True
        mock_fallback.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("apowerb.helpers.system_mailer._get_owner_token", new_callable=AsyncMock)
    async def test_no_token_falls_back_returns_false(self, mock_token):
        mock_token.return_value = None
        with patch("apowerb.helpers.system_mailer.email_sender.send_email",
                   new_callable=AsyncMock) as mock_fallback:
            ok = await system_mailer.send_system_email(
                to="u@v.com", subject="Sub", html="<b>x</b>", text="plain link"
            )
        assert ok is False
        mock_fallback.assert_awaited_once()
        # fallback uses the plain-text body when provided
        assert mock_fallback.call_args.kwargs["body"] == "plain link"

    @pytest.mark.asyncio
    @patch("apowerb.helpers.system_mailer._post_send_mail", return_value=False)
    @patch("apowerb.helpers.system_mailer._get_owner_token", new_callable=AsyncMock)
    async def test_graph_failure_falls_back(self, mock_token, mock_post):
        mock_token.return_value = "valid-token"
        with patch("apowerb.helpers.system_mailer.email_sender.send_email",
                   new_callable=AsyncMock) as mock_fallback:
            ok = await system_mailer.send_system_email(
                to="u@v.com", subject="Sub", html="<b>x</b>"
            )
        assert ok is False
        mock_fallback.assert_awaited_once()
