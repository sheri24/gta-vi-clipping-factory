import pytest

from app.youtube.urls import ChannelRef, parse_channel_ref

UC = "UCWsDFcIhY2DBi3GB5uykGXA"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://www.youtube.com/@IShowSpeed", ChannelRef("handle", "@IShowSpeed")),
        ("youtube.com/@IShowSpeed/live", ChannelRef("handle", "@IShowSpeed")),
        ("https://m.youtube.com/@itsAang/videos?x=1", ChannelRef("handle", "@itsAang")),
        ("@JoblessGarrett", ChannelRef("handle", "@JoblessGarrett")),
        (f"https://www.youtube.com/channel/{UC}", ChannelRef("id", UC)),
        (UC, ChannelRef("id", UC)),
        ("https://www.youtube.com/user/syndicateproject", ChannelRef("username", "syndicateproject")),
        ("https://www.youtube.com/c/SomeName", ChannelRef("custom", "SomeName")),
    ],
)
def test_parses_channel_refs(text, expected):
    assert parse_channel_ref(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "https://www.youtube.com/watch?v=izXcd-bU72k",
        "https://youtu.be/izXcd-bU72k",
        "https://twitch.tv/someone",
        "https://www.youtube.com/",
        "not a url at all",
    ],
)
def test_rejects_non_channels(text):
    with pytest.raises(ValueError):
        parse_channel_ref(text)
