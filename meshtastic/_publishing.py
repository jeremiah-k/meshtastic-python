"""Internal owner of the process-wide deferred publication executor.

The historical :data:`meshtastic.publishingThread` name remains a direct alias
to :data:`publishing_thread`. Keeping construction here removes publisher
ownership from the package facade while ensuring package-root and internal
consumers share one process-wide worker.
"""

from meshtastic.util import DeferredExecution

publishing_thread = DeferredExecution("publishing")
"""Process-wide worker that serializes deferred publication callbacks."""
