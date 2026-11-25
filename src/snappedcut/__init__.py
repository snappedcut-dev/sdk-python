"""The Snapped Cut client.

**The standard library and nothing else.** No dependencies, so it installs into
a locked-down CI image or a Lambda without pulling a tree behind it. The API is
small; a client for it should be readable in one sitting.

    from snappedcut import SnappedCut

    snapcut = SnappedCut()                      # reads SNAPCUT_KEY
    job = snapcut.render(open("project.zip", "rb").read())
    print(job.charged, "credits for", job.priced.seconds, "seconds")
    Path("out.webm").write_bytes(snapcut.wait(job.id))

This covers the *server* API — the cloud render queue and the account behind it.
Everything else Snapped Cut does happens in a browser on WebCodecs, and no SDK
can make Python do that.
"""

from .client import (
    VERSION,
    AccountSummary,
    Job,
    Priced,
    QueuedJob,
    SnappedCut,
    SnappedCutError,
    verify_webhook,
)

__all__ = [
    "AccountSummary",
    "Job",
    "Priced",
    "QueuedJob",
    "SnappedCut",
    "SnappedCutError",
    "verify_webhook",
]
__version__ = VERSION
