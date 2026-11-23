from datetime import datetime, timezone

from app.recording.timeline import (Anchor, estimate_chat_delay, parse_media_playlist, pts_to_utc,
                                    target_duration, utc_to_pts)

# Trimmed from a real YouTube live playlist captured in Phase 1 (itsAang, 2026-11-22).
PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:9023
#EXT-X-DATERANGE:CLASS="CUEPOINT-AD",DURATION=54.0,ID="x",START-DATE="2026-11-22T20:34:03.844+00:00"
#EXT-X-CUEPOINT:CONTEXT=abc,DURATION=60.0,EVENT=CONTINUE,TYPE=AD
#EXT-X-PROGRAM-DATE-TIME:2026-11-22T20:34:03.844+00:00
#EXTINF:2.0,
https://rr4.googlevideo.com/videoplayback/sq/9023/file.ts
#EXT-X-PROGRAM-DATE-TIME:2026-11-22T20:34:05.844+00:00
#EXTINF:2.0,
https://rr4.googlevideo.com/videoplayback/sq/9024/file.ts
#EXTINF:2.0,
https://rr4.googlevideo.com/videoplayback/sq/9025/file.ts
"""


def test_parse_assigns_pdt_to_following_segment_only():
    segs = parse_media_playlist(PLAYLIST)
    assert [s.uri.split("/")[-2] for s in segs] == ["9023", "9024", "9025"]
    assert segs[0].pdt == datetime(2026, 11, 22, 20, 34, 3, 844000, tzinfo=timezone.utc)
    assert segs[1].pdt.second == 5
    assert segs[2].pdt is None
    assert all(s.duration == 2.0 for s in segs)


def test_target_duration_and_chat_delay_estimate():
    assert target_duration(PLAYLIST) == 2.0
    assert target_duration("#EXTM3U\n") is None
    # Lacy's GTA 6 finale (normal latency): edge 31.8 s behind, 5 s segments → chat ~40 s after the moment;
    # the shooting at ~22:21:21 drew the big chat spike at 22:22:00 (39 s).
    assert estimate_chat_delay(31.8, 5) == 39.8
    assert estimate_chat_delay(4.0, 2) == 9.0          # low-latency stream
    assert estimate_chat_delay(-5, 2) == 3.0           # clock noise can't produce a negative delay


def test_pts_utc_round_trip():
    anchor = Anchor(pts=18088.182, utc=datetime(2026, 11, 22, 20, 34, 48, tzinfo=timezone.utc))
    t = pts_to_utc(18088.182 + 90.5, anchor)
    assert t == datetime(2026, 11, 22, 20, 36, 18, 500000, tzinfo=timezone.utc)
    assert abs(utc_to_pts(t, anchor) - (18088.182 + 90.5)) < 1e-6
