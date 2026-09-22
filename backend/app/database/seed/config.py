"""Generation parameters.

The historical window is **pinned to fixed dates**, never derived from
`date.today()`. Two reasons:

1.  Reproducibility. The same seed must produce the same warehouse on any
    machine on any day, or the evaluation suite's expected answers rot.
2.  Stable evaluation. Phase 13 asserts against concrete figures ("revenue in
    2025-11"). A window that slides with the wall clock would invalidate those
    the moment the calendar turned.

The window ends slightly in the past rather than exactly at the pinned "today",
which is realistic: a warehouse is loaded by a batch job, so the most recent
days are always partially or entirely absent.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

#: The end of the loaded history. Pinned deliberately — see module docstring.
DEFAULT_WINDOW_END = dt.date(2026, 8, 31)

#: Two years of history.
DEFAULT_WINDOW_START = dt.date(2024, 9, 1)

#: Customers exist before the order window opens; a business does not start
#: with zero customers. Signups are generated from this date so that a large
#: share of customers have full tenure across the window.
DEFAULT_SIGNUP_START = dt.date(2023, 3, 1)

DEFAULT_SEED = 42


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    """Scale and shape of the synthetic warehouse."""

    seed: int = DEFAULT_SEED

    window_start: dt.date = DEFAULT_WINDOW_START
    window_end: dt.date = DEFAULT_WINDOW_END
    signup_start: dt.date = DEFAULT_SIGNUP_START

    n_customers: int = 10_000
    n_orders: int = 100_000
    n_products: int = 300
    n_employees: int = 140

    #: Compound annual growth applied to daily order intensity. 22% a year is
    #: brisk but unremarkable — visible in a year-over-year chart without
    #: swamping the seasonal signal.
    annual_growth: float = 0.22

    #: Share of orders shipped to a region other than the customer's own.
    cross_region_shipping_rate: float = 0.08

    #: Orders in the final `pending_window_days` may still be pending; older
    #: ones have necessarily resolved by now.
    pending_window_days: int = 21

    def __post_init__(self) -> None:
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        if self.signup_start > self.window_start:
            raise ValueError("signup_start must be on or before window_start")
        if min(self.n_customers, self.n_orders, self.n_products, self.n_employees) <= 0:
            raise ValueError("all scale parameters must be positive")

    @property
    def total_days(self) -> int:
        """Number of days in the order window, inclusive of both endpoints."""
        return (self.window_end - self.window_start).days + 1

    def day_index(self, day: dt.date) -> int:
        """Zero-based offset of `day` from the start of the window."""
        return (day - self.window_start).days

    def date_at(self, index: int) -> dt.date:
        """Inverse of `day_index`."""
        return self.window_start + dt.timedelta(days=index)
