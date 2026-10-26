from __future__ import annotations

import hashlib
from dataclasses import replace

import httpx
import pytest

from kefu.config import Settings
from kefu.tools import LoggedHttpClient
from kefu.wecom.customer_groups import WeComCustomerGroupClient, WeComCustomerGroupError


def _client(*, member_userid: str = "consult-1"):
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(200, json={"errcode": 0, "access_token": "access-token"})
        if request.url.path.endswith("/ticket/get"):
            return httpx.Response(200, json={"errcode": 0, "ticket": "agent-ticket"})
        if request.url.path.endswith("/externalcontact/groupchat/get"):
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "group_chat": {
                        "chat_id": "external-group-id",
                        "name": "星河科技客户群",
                        "member_list": [{"type": 1, "userid": member_userid}],
                    },
                },
            )
        raise AssertionError(f"unexpected WeCom API request: {request.url.path}")

    transport = httpx.Client(transport=httpx.MockTransport(respond))
    settings = replace(
        Settings.from_env(),
        wecom_corp_id="corp-id",
        wecom_corp_secret="corp-secret",
        wecom_agent_id="100001",
    )
    return (
        WeComCustomerGroupClient(settings, http=LoggedHttpClient(client=transport)),
        calls,
        transport,
    )


def test_customer_group_lookup_checks_member_and_signs_agent_sdk_url():
    client, calls, transport = _client()
    try:
        assert client.customer_group_name_for_member("external-group-id", "consult-1") == (
            "星河科技客户群"
        )
        page_url = "https://support.example.test/issues/new"
        config = client.agent_js_sdk_config(page_url)
        expected_signature = hashlib.sha1(
            (
                f"jsapi_ticket=agent-ticket&noncestr={config['nonceStr']}"
                f"&timestamp={config['timestamp']}&url={page_url}"
            ).encode()
        ).hexdigest()
        assert config["signature"] == expected_signature
        assert "agent-ticket" not in config.values()
        assert sum(request.url.path.endswith("/gettoken") for request in calls) == 1
        assert sum(request.url.path.endswith("/ticket/get") for request in calls) == 1
    finally:
        transport.close()


def test_customer_group_lookup_rejects_non_member():
    client, _, transport = _client(member_userid="someone-else")
    try:
        with pytest.raises(WeComCustomerGroupError, match="不属于"):
            client.customer_group_name_for_member("external-group-id", "consult-1")
    finally:
        transport.close()


def test_customer_group_lookup_rejects_mismatched_chat_id():
    client, calls, transport = _client()
    original_request = client._post_json

    def mismatched_chat_id(path, body, *, operation):
        payload = original_request(path, body, operation=operation)
        return {
            **payload,
            "group_chat": {**payload["group_chat"], "chat_id": "another-group-id"},
        }

    client._post_json = mismatched_chat_id
    try:
        with pytest.raises(WeComCustomerGroupError, match="不匹配"):
            client.customer_group_name_for_member("external-group-id", "consult-1")
        assert calls
    finally:
        transport.close()
