"""Official live chat via gRPC liveChatMessages.streamList (API key, 5 quota units per connection).

Measured in Phase 4: YouTube closes each stream after ~10.5 s, so this costs ~1,780 units per
stream-hour. That's why it's the fallback, not the default. Resuming with next_page_token
loses nothing; a few messages repeat and are dropped by the store's msg_id dedupe.
"""

import threading
from datetime import datetime

import grpc

from app.chat.store import ChatMessage, ChatStore
from app.youtube.api import QuotaExceeded, QuotaLedger
from app.youtube.proto import stream_list_pb2 as pb
from app.youtube.proto import stream_list_pb2_grpc as pbg

STREAM_COST = 5
Type = pb.LiveChatMessageSnippet.TypeWrapper.Type
KIND_BY_TYPE = {
    "TEXT_MESSAGE_EVENT": "text",
    "SUPER_CHAT_EVENT": "paid",
    "SUPER_STICKER_EVENT": "sticker",
    "NEW_SPONSOR_EVENT": "membership",
    "MEMBER_MILESTONE_CHAT_EVENT": "membership",
    "MEMBERSHIP_GIFTING_EVENT": "membership",
    "GIFT_MEMBERSHIP_RECEIVED_EVENT": "membership",
    "GIFT_EVENT": "gift",
}


def to_us(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1_000_000)


def convert(item) -> ChatMessage | None:
    kind = KIND_BY_TYPE.get(Type.Name(item.snippet.type)) if item.snippet.type else None
    if not kind or not item.snippet.published_at:
        return None
    return ChatMessage(
        msg_id=item.id, published_us=to_us(item.snippet.published_at), kind=kind,
        author_id=item.snippet.author_channel_id or None,
        author_name=item.author_details.display_name or None,
        text=item.snippet.display_message or None,
    )


def default_stub():
    channel = grpc.secure_channel("dns:///youtube.googleapis.com:443", grpc.ssl_channel_credentials())
    return pbg.V3DataLiveChatMessageServiceStub(channel)


class OfficialChatSource:
    name = "official"

    def __init__(self, api_key: str, ledger: QuotaLedger, stub=None):
        self.api_key = api_key
        self.ledger = ledger
        self._stub = stub

    @property
    def stub(self):
        if self._stub is None:
            self._stub = default_stub()
        return self._stub

    def available(self) -> bool:
        return bool(self.api_key) and self.ledger.used() + STREAM_COST <= self.ledger.daily_budget

    def run(self, session: dict, store: ChatStore, stop: threading.Event) -> tuple[str, int, str | None]:
        """Returns (result, messages_added, error). result: stopped | ended | quota | error."""
        chat_id = session.get("live_chat_id")
        if not chat_id:
            return "error", 0, "session has no live_chat_id (chat disabled?)"
        token, added = None, 0
        while not stop.is_set():
            try:
                self.ledger.spend(STREAM_COST)
            except QuotaExceeded as e:
                return "quota", added, str(e)
            req = pb.LiveChatMessageListRequest(part=["snippet", "authorDetails"],
                                                live_chat_id=chat_id, page_token=token)
            try:
                for resp in self.stub.StreamList(req, metadata=(("x-goog-api-key", self.api_key),),
                                                 timeout=120):
                    batch = [m for m in (convert(i) for i in resp.items) if m]
                    added += store.add(session["id"], batch, self.name)
                    if resp.next_page_token:
                        token = resp.next_page_token
                    if resp.offline_at:
                        return "ended", added, None
                    if stop.is_set():
                        return "stopped", added, None
            except grpc.RpcError as e:
                if "quota" in (e.details() or "").lower():
                    self.ledger.mark_exhausted()
                    return "quota", added, e.details()
                if e.code() != grpc.StatusCode.DEADLINE_EXCEEDED:
                    return "error", added, f"{e.code().name}: {e.details()}"
        return "stopped", added, None
