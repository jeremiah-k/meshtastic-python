"""Internal owner of the process-wide deferred publication executor.

The historical :data:`meshtastic.publishingThread` name remains a direct alias
to :data:`publishing_thread`. Keeping construction here removes publisher
ownership from the package facade while ensuring package-root and internal
consumers share one process-wide worker.
"""

from meshtastic.util import DeferredExecution

publishing_thread = DeferredExecution._create_lazy("publishing")
"""Process-wide executor that starts its worker on the first queued callback."""
