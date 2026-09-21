from __future__ import annotations

from kefu.wecom.deferred_reply import DeferredPassiveReplyStore
from kefu.wecom.transport import InboundEvent, InboundTextPart
from tests.conftest import DeskContext


def test_deferred_callback_context_is_visible_to_a_second_store_instance(
    desk_context: DeskContext,
) -> None:
    raw_frame = {
        "headers": {"req_id": "persisted-callback-1"},
        "body": {"msgid": "developer-persisted-1", "chattype": "group"},
    }
    event = InboundEvent(
        msgid="developer-persisted-1",
        sender_userid="dev-a",
        chatid="chat-dev-a",
        chattype="group",
        parts=(InboundTextPart("研发回复"),),
        metadata={"wecom_raw_frame": raw_frame},
    )
    writer = DeferredPassiveReplyStore(desk_context.session_factory, ttl_seconds=60)
    reader = DeferredPassiveReplyStore(desk_context.session_factory, ttl_seconds=60)

    saved = writer.save(event, "abcdefg")

    assert saved is not None
    assert reader.has_match("ABCDEFG") is True
    pending = reader.take("ABCDEFG")
    assert pending is not None
    assert pending.req_id == "persisted-callback-1"
    assert pending.event.msgid == event.msgid
    assert pending.event.metadata == {"wecom_raw_frame": raw_frame}

    reader.acknowledge(pending)
    assert len(writer) == 0
