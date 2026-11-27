"""Self-improvement foundation: run tagging, the improvement budget and the off-limits list.

* :mod:`ts_harness.improvement.versions` records which workflow, fragment, skill and model
  versions (and which variants) every agent step and coding job ran with (``_versions``);
* :mod:`ts_harness.improvement.budget` is the monthly model-cost cap for improvement work,
  apart from delivery budgets;
* :mod:`ts_harness.improvement.off_limits` lists what the loop may never change on its own;
* the target and guard metrics live with the delivery metrics
  (:mod:`ts_harness.workflows.delivery_metrics`, :mod:`ts_harness.improvement.guard`).

See docs/runbooks/self-improvement.md.
"""

from ts_harness.improvement.off_limits import OFF_LIMITS, is_off_limits

__all__ = ["OFF_LIMITS", "is_off_limits"]
