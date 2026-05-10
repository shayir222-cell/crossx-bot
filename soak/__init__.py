"""CrossX soak validation package — observability integrity audit.

Public API:
    from soak.config import SoakConfig
    from soak.validator import SoakValidator
    from soak.report import build_report

The soak validator is OFF by default. It must be explicitly enabled
via ENABLE_SOAK_VALIDATION=true (read by bot.py at startup).

Trading is never affected by soak failures: every operation is
exception-isolated, and the validator runs in its own daemon thread.
"""

from soak.config import SoakConfig
from soak.validator import SoakValidator
from soak.report import build_report

__all__ = ['SoakConfig', 'SoakValidator', 'build_report']
