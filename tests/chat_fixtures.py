"""Synthetic yt-dlp live_chat lines mirroring the real structure captured in Phase 4."""

import json

T0_US = 1_795_382_700_000_000   # capture start (YouTube clock)


def text_line(msg_id, offset_ms, author="UCa", text="hi", emoji=None, ts_us=None):
    runs = [{"text": text}]
    if emoji:
        runs.append({"emoji": {"emojiId": emoji, "shortcuts": [f":{emoji}:"] if emoji.isascii() else []}})
    body = {"id": msg_id, "timestampUsec": str(ts_us or T0_US + offset_ms * 1000),
            "authorExternalChannelId": author, "authorName": {"simpleText": f"@{author}"},
            "message": {"runs": runs}}
    return json.dumps({"replayChatItemAction": {"actions": [
        {"clickTrackingParams": "x", "addChatItemAction": {"item": {"liveChatTextMessageRenderer": body}}}]},
        "videoOffsetTimeMsec": str(offset_ms), "isLive": True})


def paid_line(msg_id, offset_ms, author="UCp"):
    body = {"id": msg_id, "timestampUsec": str(T0_US + offset_ms * 1000), "authorExternalChannelId": author,
            "authorName": {"simpleText": "@rich"}, "purchaseAmountText": {"simpleText": "$5.00"},
            "message": {"runs": [{"text": "GG"}]}}
    return json.dumps({"replayChatItemAction": {"actions": [
        {"addChatItemAction": {"item": {"liveChatPaidMessageRenderer": body}}}]},
        "videoOffsetTimeMsec": str(offset_ms), "isLive": True})


def gift_line(msg_id, offset_ms):
    body = {"id": msg_id, "text": {"content": "sent LFG"}, "authorName": {"content": "@gifter "}}
    return json.dumps({"replayChatItemAction": {"actions": [
        {"addChatItemAction": {"item": {"giftMessageViewModel": body}}}]},
        "videoOffsetTimeMsec": str(offset_ms), "isLive": True})


def remove_line(offset_ms):
    return json.dumps({"replayChatItemAction": {"actions": [
        {"removeChatItemAction": {"targetItemId": "gone"}}]},
        "videoOffsetTimeMsec": str(offset_ms), "isLive": True})
