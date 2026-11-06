"""Domain-independent, policy-governed A-MEM runtime."""
from .system import PrivacyMemorySystem, AccessDenied, Context
from .permissions import PermissionBootstrap, PermissionSourceEvent, PermissionUpdater
from .governance import CapabilityCandidate, PolicyGovernor
from .learning import AuthorizationLearner
from .answering import PermissionQueryService

__all__ = ["PrivacyMemorySystem", "AccessDenied", "Context", "PermissionBootstrap",
           "PermissionSourceEvent", "PermissionUpdater", "CapabilityCandidate", "PolicyGovernor", "AuthorizationLearner", "PermissionQueryService"]

from .service_v21 import MemoryService, JSONClient, in_process_transport
from .retrieval_v21 import RetrievalConfig
from .deduplication_v21 import ScopedDeduplicator
__all__ += ["MemoryService", "JSONClient", "in_process_transport", "RetrievalConfig", "ScopedDeduplicator"]
