"""harness.skill — skill-as-container execution package.

Split from the former harness/skill_*.py modules + browser_tools/workflow_skill.py:
  registry · dispatch · control · pause · health · heal · autoheal ·
  contract · visual_contract · workflow

Import from the specific submodule, e.g.
  from harness.skill.registry import SkillRegistry
  from harness.skill.dispatch import maybe_run_skill_fast_path
A few leaf conveniences are re-exported here for `from harness.skill import X`.
"""
from harness.skill.registry import Skill, SkillRegistry  # noqa: F401
from harness.skill.workflow import (  # noqa: F401
    build_execute_params,
    check_persisted_contract,
    check_success_contract,
    run_skill_workflow,
)
