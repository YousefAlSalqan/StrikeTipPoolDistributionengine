"""
Data models for the Tip Pool Distribution Engine.

Entities marked "owned by Strikepay" are supplied as inputs on every call.
Entities marked "owned by this system" are persisted by this system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class CompensationType(str, Enum):
    COMMISSION = "commission"
    HOURLY = "hourly"
    BOOTH_RENTAL = "booth_rental"


class StrategyType(str, Enum):
    DIRECT = "direct"
    TIP_OUT = "tip_out"
    POOL = "pool"
    MULTI_SERVICE = "multi_service"


# ---------------------------------------------------------------------------
# Strikepay-owned entities (supplied as inputs)
# ---------------------------------------------------------------------------

@dataclass
class StaffMember:
    """An employee or contractor at a salon. Supports dual roles."""
    id: str
    name: str
    roles: list[str]                    # e.g. ["stylist"] or ["owner", "stylist"]
    compensation_type: CompensationType
    seniority_level: str                # e.g. "junior", "mid", "senior"


@dataclass
class Shift:
    """A time-bounded work record linking a staff member to a salon."""
    id: str
    staff_id: str
    salon_id: str
    clock_in: datetime
    clock_out: datetime
    break_minutes: int = 0
    active_role: Optional[str] = None  # role being performed during this shift

    @property
    def hours_worked(self) -> Decimal:
        total_minutes = (
            (self.clock_out - self.clock_in).total_seconds() / 60
            - self.break_minutes
        )
        return Decimal(str(round(max(total_minutes, 0) / 60, 6)))


@dataclass
class ShiftRecord:
    """Pairs a staff member with their shift; the unit of a shift snapshot."""
    staff: StaffMember
    shift: Shift


@dataclass
class AppointmentContributor:
    """One staff member's contribution to a multi-provider appointment."""
    staff_id: str
    role: str
    service_value_cents: int


@dataclass
class Appointment:
    """A booked client service, potentially involving multiple staff members."""
    id: str
    salon_id: str
    contributors: list[AppointmentContributor]
    start_time: datetime
    end_time: datetime
    client_ref: Optional[str] = None


@dataclass
class TipTransaction:
    """An incoming tip event from Strikepay."""
    id: str
    amount_cents: int       # canonical representation; all arithmetic uses cents
    currency: str
    timestamp: datetime
    source: str             # "qr" | "nfc" | "card"
    salon_id: str
    linked_appointment_id: Optional[str] = None
    linked_staff_id: Optional[str] = None   # set when tip is directed at one person


# ---------------------------------------------------------------------------
# This system's entities
# ---------------------------------------------------------------------------

@dataclass
class RuleSet:
    """
    The complete, versioned configuration that defines how tips are distributed
    at a particular salon. Immutable once saved — policy changes create a new
    version with a later effective_date.
    """
    salon_id: str
    effective_date: datetime
    default_strategy: StrategyType

    # Weights applied in pool and tip-out calculations
    role_weights: dict[str, Decimal]            # role name -> multiplier (≥ 0)
    seniority_multipliers: dict[str, Decimal]   # seniority level -> multiplier (≥ 0)

    # Tip-out configuration
    tip_out_percentage: Decimal                 # fraction in [0, 1]
    support_roles: list[str]                    # roles that receive the cascade

    # Which compensation types participate in the pool
    eligibility_by_compensation: dict[str, bool]  # CompensationType.value -> bool

    # Strategy selection rules
    multi_service_for_multi_provider: bool = True

    # Assigned by the rule set store on save; None before persistence
    id: Optional[str] = None


@dataclass
class AllocationEntry:
    """One staff member's share of a single tip, in cents."""
    staff_id: str
    amount_cents: int


@dataclass
class CascadeStep:
    """One arithmetic operation recorded in the tip-out cascade trail."""
    description: str
    amount_cents: int
    from_staff_id: Optional[str] = None
    to_staff_id: Optional[str] = None


@dataclass
class CascadeRecord:
    """Complete step-by-step record of a tip-out cascade."""
    tip_out_percentage: Decimal
    original_primary_cents: int
    reduced_primary_cents: int
    total_cascaded_cents: int
    steps: list[CascadeStep] = field(default_factory=list)


@dataclass
class AllocationRecord:
    """Intermediate result produced by an allocation strategy function."""
    entries: list[AllocationEntry]
    cascade_record: Optional[CascadeRecord] = None


@dataclass
class ReconciliationProof:
    """Proves that per-staff allocations sum to the original tip total."""
    tip_total_cents: int
    allocation_total_cents: int
    balanced: bool


@dataclass
class DistributionResult:
    """
    Ledger-ready output of distribute_tip. Self-contained: every field
    necessary to reconstruct the allocation is captured here.
    """
    tip_id: str
    rule_set_version_id: str
    shift_snapshot: list[ShiftRecord]           # captured at allocation time
    per_staff_amounts: dict[str, int]           # staff_id -> cents
    strategy_used: StrategyType
    rounding_method: str
    timestamp: datetime
    reconciliation_proof: ReconciliationProof
    cascade_record: Optional[CascadeRecord] = None
    id: Optional[str] = None                   # assigned by the ledger on append


@dataclass
class ClawbackEntry:
    """One staff member's portion of a clawback."""
    staff_id: str
    clawback_cents: int


@dataclass
class ClawbackRecord:
    """Immutable ledger entry documenting a proportional reversal."""
    original_distribution_id: str
    charged_back_cents: int
    per_staff_clawbacks: list[ClawbackEntry]
    timestamp: datetime
    id: Optional[str] = None                   # assigned by the ledger on append


@dataclass
class BalanceAdjustment:
    """Result of applying a clawback to one staff member's pending balance."""
    staff_id: str
    previous_balance_cents: int
    applied_clawback_cents: int
    new_balance_cents: int
    residual_cents: int                         # carried forward against future allocations


@dataclass
class BatchReconciliation:
    """Batch-level proof that allocations equal incoming tips."""
    tip_ids: list[str]
    total_tips_cents: int
    total_allocated_cents: int
    balanced: bool
    per_tip_results: list[DistributionResult]
