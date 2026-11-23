"""Telling "the internet is down" apart from real failures.

An outage must never use up a job's retry attempts (a past-stream download, an upload): the work just
waits and tries again once the connection is back. Real failures (a deleted video, a refused upload)
still count, so they end as 'failed' instead of retrying forever.
"""

import httpx

# What yt-dlp, httpx, httplib2 (YouTube uploads), google-auth and Windows say when the network is the problem.
NETWORK_HINTS = ("getaddrinfo", "name resolution", "timed out", "timeout", "connection reset", "connection aborted",
                 "connection refused", "network is unreachable", "no route to host", "remote end closed",
                 "connecterror", "readerror", "unable to find the server", "servernotfounderror",
                 "failed to establish a new connection", "max retries exceeded",
                 "http error 502", "http error 503", "http error 504")


def is_network_error(e: BaseException) -> bool:
    return isinstance(e, (httpx.TransportError, ConnectionError, TimeoutError)) or \
        any(h in str(e).lower() for h in NETWORK_HINTS)
