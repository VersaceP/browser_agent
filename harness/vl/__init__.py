"""harness.vl — VL (vision-language) capability package.

Split from the former single harness/vl.py:
  - core    : visual_verify modes / prompt builder / per-mode finalizers
  - arbiter : Role D — global visual arbitration of browser_call failures
  - locate  : Role A — AXTree-blind visual locate + bbox→canonical-id promotion

This __init__ re-exports the core API so `from harness.vl import X` keeps working
after the split; the sub-roles are imported from `harness.vl.arbiter` /
`harness.vl.locate`.
"""
from harness.vl.core import *  # noqa: F401,F403
from harness.vl.core import (  # noqa: F401  (underscore names are not covered by *)
    _finalize_captcha_solve,
    _finalize_contract_verify,
    _finalize_overlay_classify,
    _finalize_repair_absence,
    _finalize_visual_locate,
)
