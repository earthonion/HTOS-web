"""Per-(title_id, operation) download-availability delay.

Structure: {(TITLE_ID, operation): delay_seconds}. Lookup normalises the
title ID to upper-case and the operation to lower-case.

Intent: friction against iterative cheat-testing workflows. The job runs
and completes normally; the download button just doesn't unlock until
`completed_at + delay` has passed. Legit users wait once. Cheaters
iterating through hundreds of test values lose their tight loop.

Edit this file and reload htos to change the list.
"""

JOB_THROTTLES = {}


def throttle_delay(title_id: str, operation: str) -> int:
    """Seconds of download-delay for this job, or 0 if not throttled."""
    if not title_id or not operation:
        return 0
    return JOB_THROTTLES.get((title_id.upper(), operation.lower()), 0)
