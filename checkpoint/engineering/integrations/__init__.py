"""Internal integration reservations, disconnected from the application routes.

All adapters are unimplemented and fail closed. These are not official Taobao or
WeChat API schemas. Nothing in this package grants a model transaction authority.
"""

from .adapters import DisabledTaobaoAdapter, DisabledWeChatAdapter
from .contracts import CapabilityUnavailable

__all__ = ["CapabilityUnavailable", "DisabledTaobaoAdapter", "DisabledWeChatAdapter"]
