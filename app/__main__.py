"""Start the clip factory:   .\\.venv\\Scripts\\python -m app        (dashboard: http://127.0.0.1:8000/)

Why not plain `uvicorn app.main:app`: on Ctrl+C uvicorn returns, but Python then waits for any job still
running in a worker thread (a Whisper transcription, a chat-replay download) before it exits; that took
over a minute in practice, with the scheduler still logging. A second Ctrl+C skips the app's own cleanup
altogether. Every step resumes where it was on the next start (Database.mark_interrupted), so once the
server has stopped there is nothing worth waiting for: exit right away. Windows' kill-on-close job ends
yt-dlp/ffmpeg children with us.
"""

import logging
import os
import sys

import uvicorn


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    try:
        uvicorn.run("app.main:app", host="127.0.0.1", port=port, timeout_graceful_shutdown=5)
    except KeyboardInterrupt:
        pass          # uvicorn re-raises the Ctrl+C it handled once the server is down
    finally:
        print("Clip factory stopped. Unfinished work continues from where it was on the next start.", flush=True)
        logging.shutdown()
        sys.stderr.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
