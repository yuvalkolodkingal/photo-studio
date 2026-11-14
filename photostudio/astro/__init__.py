"""Deep-sky / Milky-Way stacking (a Sequator-style workflow) for untracked camera frames.

    engine   registration (stars, bright-star tracking + ECC homography, nudge detection) and a streaming,
             multicore two-pass sigma-clipped / winsorized weighted integration with split-half stacks
    post     finishing: light-pollution (gradient) removal, star colour calibration, asinh stretch with
             auto brightness and HDR highlight protection, star enhancement, star-protected noise reduction,
             split-half validation of faint sources
    gui      local browser GUI (``photo astro gui``) laid out like Sequator
"""
from .engine import StackOptions, StackResult, stack  # noqa: F401
from .post import FinishOptions, finish  # noqa: F401
