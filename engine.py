"""
Core allocation engine — pure Python, no I/O.

Sections follow the function-responsibility spec:
  1. Allocation strategies
  2. Weighting and math
  3. Rule set logic
  4. Distribution orchestrator
  5. Clawback
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from models import (
    AllocationEntry,
    AllocationRecord,
    Appointment,
    BalanceAdjustment,
    BatchReconciliation,
    CascadeRecord,
    CascadeStep,
    ClawbackEntry,
    ClawbackRecord,
    CompensationType,
    DistributionResult,
    ReconciliationProof,
    RuleSet,
    ShiftRecord,
    StaffMember,
    StrategyType,
    TipTransaction,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class DistributionError(Exception):
    """Base error for all engine failures."""


class ValidationError(DistributionError):
    """Rule set failed validation; carries every defect found."""
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class NoEligibleStaffError(DistributionError):
    """No staff passed the eligibility filter; tip cannot be allocated."""


class ZeroTotalWeightError(DistributionError):
    """Total worker weight is zero; division is undefined."""


class RuleSetNotFoundError(DistributionError):
    """No rule set could be resolved for the given salon and timestamp."""


class ReconciliationError(DistributionError):
    """Allocated cents do not equal tip cents; indicates a system bug."""


# ---------------------------------------------------------------------------
# Section 2 — Weighting and Math
# ---------------------------------------------------------------------------

_KNOWN_STRATEGIES = frozenset(s.value for s in StrategyType)
_KNOWN_COMP_TYPES = frozenset(c.value for c in CompensationType)


def compute_worker_weight(
    hours_worked: Decimal,
    role_multiplier: Decimal,
    seniority_multiplier: Decimal,
) -> Decimal:
    """
    hours × role_multiplier × seniority_multiplier.
    Multiplicative so factors compound; zero hours yields zero weight.
    """
    if hours_worked < 0 or role_multiplier < 0 or seniority_multiplier < 0:
        raise DistributionError(
            f"Negative inputs are data corruption: hours={hours_worked}, "
            f"role={role_multiplier}, seniority={seniority_multiplier}."
        )
    return hours_worked * role_multiplier * seniority_multiplier


def compute_total_weight(weights: list[Decimal]) -> Decimal:
    """Sum of all worker weights; zero on empty list (caller detects divide-by-zero)."""
    return sum(weights, Decimal("0"))


def compute_share(
    worker_weight: Decimal,
    total_weight: Decimal,
    tip_amount_cents: int,
) -> Decimal:
    """
    Raw fractional share — (worker_weight / total_weight) × tip_amount_cents.
    Returns exact Decimal; rounding is NOT done here.
    """
    if total_weight == 0:
        raise ZeroTotalWeightError(
            "Total weight is zero; cannot compute share — caller must guard this."
        )
    if worker_weight == 0:
        return Decimal("0")
    return (worker_weight / total_weight) * Decimal(tip_amount_cents)


def largest_remainder_round(
    raw_shares: list[Decimal],
    total_cents: int,
) -> list[int]:
    """
    Converts fractional cent shares to integers that sum exactly to total_cents.

    Algorithm:
      1. Floor every share.
      2. Distribute leftover cents one at a time to the largest fractional remainders.
      3. Ties broken by stable index order — deterministic on identical inputs.

    This is the single most critical arithmetic function: every multi-recipient
    allocation passes through it, and a bug here corrupts all of them.
    """
    if not raw_shares:
        if total_cents != 0:
            raise DistributionError(
                f"Empty share list but total_cents={total_cents}; cannot distribute."
            )
        return []

    floors = [int(s) for s in raw_shares]
    remainders = [s - int(s) for s in raw_shares]
    leftover = total_cents - sum(floors)

    # Sort indices by remainder descending; Python's sort is stable so equal
    # remainders preserve their original order.
    ranked = sorted(range(len(remainders)), key=lambda i: remainders[i], reverse=True)
    for i in range(leftover):
        floors[ranked[i]] += 1

    assert sum(floors) == total_cents, "largest_remainder_round invariant violated"
    return floors


def compute_service_proportions(appointment: Appointment) -> dict[str, Decimal]:
    """
    Per-staff fraction of total appointment service value.
    Raises if any value is negative or the total is zero.
    """
    for c in appointment.contributors:
        if c.service_value_cents < 0:
            raise DistributionError(
                f"Negative service value for staff {c.staff_id}: {c.service_value_cents}."
            )
    total = sum(c.service_value_cents for c in appointment.contributors)
    if total == 0:
        raise DistributionError(
            "Service values sum to zero; equal-split fallback is the caller's policy choice."
        )
    return {
        c.staff_id: Decimal(c.service_value_cents) / Decimal(total)
        for c in appointment.contributors
    }


def apply_tip_out_cascade(
    primary_allocation_cents: int,
    support_staff: list[tuple[StaffMember, Decimal]],
    tip_out_percentage: Decimal,
) -> tuple[int, list[AllocationEntry], CascadeRecord]:
    """
    Deducts tip_out_percentage from the primary stylist's share and distributes
    it among support_staff weighted by their role weights.

    Returns:
        reduced_primary_cents   — primary share after deduction
        support_entries         — per-support-staff allocations
        cascade_record          — step-by-step audit trail
    """
    if not support_staff or tip_out_percentage == 0:
        record = CascadeRecord(
            tip_out_percentage=tip_out_percentage,
            original_primary_cents=primary_allocation_cents,
            reduced_primary_cents=primary_allocation_cents,
            total_cascaded_cents=0,
            steps=[CascadeStep(
                description="Cascade skipped: no eligible support staff or zero tip-out percentage.",
                amount_cents=0,
            )],
        )
        return primary_allocation_cents, [], record

    steps: list[CascadeStep] = []

    cascade_cents = int(Decimal(primary_allocation_cents) * tip_out_percentage)
    reduced_primary = primary_allocation_cents - cascade_cents

    steps.append(CascadeStep(
        description=(
            f"Deducted {tip_out_percentage * 100:.2f}% "
            f"({cascade_cents}¢) from primary allocation of {primary_allocation_cents}¢."
        ),
        amount_cents=cascade_cents,
    ))

    total_support_weight = sum(w for _, w in support_staff)
    raw_shares = [
        (w / total_support_weight) * Decimal(cascade_cents)
        for _, w in support_staff
    ]
    rounded = largest_remainder_round(raw_shares, cascade_cents)

    support_entries: list[AllocationEntry] = []
    for (member, weight), amount in zip(support_staff, rounded):
        support_entries.append(AllocationEntry(staff_id=member.id, amount_cents=amount))
        steps.append(CascadeStep(
            description=f"Cascaded to {member.name} (role weight={weight}).",
            to_staff_id=member.id,
            amount_cents=amount,
        ))

    record = CascadeRecord(
        tip_out_percentage=tip_out_percentage,
        original_primary_cents=primary_allocation_cents,
        reduced_primary_cents=reduced_primary,
        total_cascaded_cents=cascade_cents,
        steps=steps,
    )
    return reduced_primary, support_entries, record


# ---------------------------------------------------------------------------
# Section 3 — Rule Set Logic
# ---------------------------------------------------------------------------

def validate_rule_set(rule_set: RuleSet) -> None:
    """
    Validates a candidate rule set and raises ValidationError listing every
    defect found — not just the first one.
    """
    errors: list[str] = []

    for role, weight in rule_set.role_weights.items():
        if weight < 0:
            errors.append(f"Role weight for '{role}' is negative ({weight}).")

    for level, mult in rule_set.seniority_multipliers.items():
        if mult < 0:
            errors.append(f"Seniority multiplier for '{level}' is negative ({mult}).")

    if not (Decimal("0") <= rule_set.tip_out_percentage <= Decimal("1")):
        errors.append(
            f"tip_out_percentage must be in [0, 1]; got {rule_set.tip_out_percentage}."
        )

    for comp_type in rule_set.eligibility_by_compensation:
        if comp_type not in _KNOWN_COMP_TYPES:
            errors.append(f"Unknown compensation type in eligibility rules: '{comp_type}'.")

    if rule_set.default_strategy.value not in _KNOWN_STRATEGIES:
        errors.append(f"Unknown default strategy: '{rule_set.default_strategy}'.")

    if rule_set.effective_date is None:
        errors.append("effective_date is missing.")

    if errors:
        raise ValidationError(errors)


def is_staff_eligible(
    shift_record: ShiftRecord,
    rule_set: RuleSet,
    tip: TipTransaction,
) -> bool:
    """
    Returns True iff the staff member qualifies to receive a share of this tip.

    Checks (in order):
      1. Compensation-type eligibility per rule set.
      2. On-shift at the moment the tip was given.
      3. The role being performed is recognised in the rule set's role weights.
    """
    staff = shift_record.staff
    shift = shift_record.shift

    # 1. Compensation-type filter
    comp_eligible = rule_set.eligibility_by_compensation.get(
        staff.compensation_type.value, True
    )
    if not comp_eligible:
        return False

    # 2. Shift coverage — tip timestamp must fall within the shift
    if not (shift.clock_in <= tip.timestamp <= shift.clock_out):
        return False

    # 3. Role recognition — for dual-role staff, use the shift's declared active role
    active_role = shift.active_role or (staff.roles[0] if staff.roles else None)
    if active_role is None or active_role not in rule_set.role_weights:
        return False

    return True


# ---------------------------------------------------------------------------
# Section 1 — Allocation Strategies
# ---------------------------------------------------------------------------

def allocate_direct(tip: TipTransaction, stylist: StaffMember) -> AllocationRecord:
    """Full tip amount to one named stylist; no division or cascade."""
    return AllocationRecord(
        entries=[AllocationEntry(staff_id=stylist.id, amount_cents=tip.amount_cents)]
    )


def allocate_tip_out(
    tip: TipTransaction,
    primary_stylist: StaffMember,
    support_staff: list[tuple[StaffMember, Decimal]],
    tip_out_percentage: Decimal,
) -> AllocationRecord:
    """
    Primary stylist receives the tip minus the tip-out percentage; the deducted
    amount cascades to support staff weighted by their role weights.
    """
    reduced_primary, cascade_entries, cascade_record = apply_tip_out_cascade(
        primary_allocation_cents=tip.amount_cents,
        support_staff=support_staff,
        tip_out_percentage=tip_out_percentage,
    )
    entries = [AllocationEntry(staff_id=primary_stylist.id, amount_cents=reduced_primary)]
    entries.extend(cascade_entries)
    return AllocationRecord(entries=entries, cascade_record=cascade_record)


def allocate_pool(
    tip: TipTransaction,
    eligible_staff: list[ShiftRecord],
    rule_set: RuleSet,
) -> AllocationRecord:
    """
    Distributes the tip across all eligible staff, weighted by hours × role × seniority.
    Raises NoEligibleStaffError if the list is empty or all weights are zero.
    """
    if not eligible_staff:
        raise NoEligibleStaffError(
            f"No eligible staff for pool allocation of tip {tip.id}."
        )

    weights: list[Decimal] = []
    for sr in eligible_staff:
        active_role = sr.shift.active_role or sr.staff.roles[0]
        role_mult = rule_set.role_weights.get(active_role, Decimal("1"))
        seniority_mult = rule_set.seniority_multipliers.get(
            sr.staff.seniority_level, Decimal("1")
        )
        weights.append(compute_worker_weight(sr.shift.hours_worked, role_mult, seniority_mult))

    total_weight = compute_total_weight(weights)
    if total_weight == 0:
        raise NoEligibleStaffError(
            f"All eligible staff have zero weight for tip {tip.id} "
            "(everyone has zero hours or zero role multiplier)."
        )

    raw_shares = [compute_share(w, total_weight, tip.amount_cents) for w in weights]
    rounded = largest_remainder_round(raw_shares, tip.amount_cents)

    entries = [
        AllocationEntry(staff_id=sr.staff.id, amount_cents=cents)
        for sr, cents in zip(eligible_staff, rounded)
    ]
    return AllocationRecord(entries=entries)


def allocate_multi_service(
    tip: TipTransaction,
    appointment: Appointment,
    rule_set: RuleSet,
    support_staff: Optional[list[tuple[StaffMember, Decimal]]] = None,
) -> AllocationRecord:
    """
    Splits a tip among appointment contributors weighted by per-staff service value.
    Applies a tip-out cascade to each contributor's share if support_staff is provided
    and rule_set.tip_out_percentage > 0.
    """
    if len(appointment.contributors) == 1:
        single = appointment.contributors[0]
        return AllocationRecord(
            entries=[AllocationEntry(staff_id=single.staff_id, amount_cents=tip.amount_cents)]
        )

    proportions = compute_service_proportions(appointment)
    raw_shares = [
        proportions[c.staff_id] * Decimal(tip.amount_cents)
        for c in appointment.contributors
    ]
    rounded = largest_remainder_round(raw_shares, tip.amount_cents)

    if not support_staff or rule_set.tip_out_percentage == 0:
        entries = [
            AllocationEntry(staff_id=c.staff_id, amount_cents=cents)
            for c, cents in zip(appointment.contributors, rounded)
        ]
        return AllocationRecord(entries=entries)

    # Apply cascade to each contributor's share independently
    all_entries: list[AllocationEntry] = []
    first_cascade: Optional[CascadeRecord] = None

    for contributor, primary_cents in zip(appointment.contributors, rounded):
        reduced, cascaded, cascade_record = apply_tip_out_cascade(
            primary_allocation_cents=primary_cents,
            support_staff=support_staff,
            tip_out_percentage=rule_set.tip_out_percentage,
        )
        all_entries.append(AllocationEntry(staff_id=contributor.staff_id, amount_cents=reduced))
        all_entries.extend(cascaded)
        if first_cascade is None:
            first_cascade = cascade_record

    return AllocationRecord(entries=all_entries, cascade_record=first_cascade)


def select_strategy(
    rule_set: RuleSet,
    tip: TipTransaction,
    appointment: Optional[Appointment],
) -> tuple[StrategyType, dict]:
    """
    Determines which strategy to apply and returns any strategy-specific context.

    Priority:
      1. Multi-service: appointment exists with multiple contributors.
      2. Direct: tip is explicitly linked to one staff member.
      3. Default from the rule set.

    Raises if the rule set default is not a known strategy.
    """
    if (
        appointment is not None
        and len(appointment.contributors) > 1
        and rule_set.multi_service_for_multi_provider
    ):
        return StrategyType.MULTI_SERVICE, {"appointment": appointment}

    if tip.linked_staff_id is not None:
        return StrategyType.DIRECT, {}

    if rule_set.default_strategy.value not in _KNOWN_STRATEGIES:
        raise DistributionError(
            f"Rule set default_strategy '{rule_set.default_strategy}' is not recognised. "
            "Fix the rule set — silent defaults hide configuration bugs."
        )
    return rule_set.default_strategy, {}


# ---------------------------------------------------------------------------
# Section 4 — Distribution Orchestrator
# ---------------------------------------------------------------------------

def distribute_tip(
    tip: TipTransaction,
    shift_snapshot: list[ShiftRecord],
    rule_set: RuleSet,
    appointment: Optional[Appointment] = None,
) -> DistributionResult:
    """
    End-to-end allocation of a single tip.

    Sequence (order is contractual):
      1. Strategy selection
      2. Eligibility filtering
      3. Share computation + cascade (inside strategy function)
      4. Rounding via largest_remainder_round (inside strategy function)
      5. Reconciliation proof
    """
    if not rule_set.id:
        raise DistributionError(
            "Rule set must have a persisted ID before distribute_tip is called."
        )

    strategy_type, strategy_kwargs = select_strategy(rule_set, tip, appointment)

    eligible = [sr for sr in shift_snapshot if is_staff_eligible(sr, rule_set, tip)]

    allocation: AllocationRecord

    if strategy_type == StrategyType.DIRECT:
        if tip.linked_staff_id:
            primary = next(
                (sr.staff for sr in shift_snapshot if sr.staff.id == tip.linked_staff_id),
                None,
            )
            if primary is None:
                raise DistributionError(
                    f"Directly tipped staff {tip.linked_staff_id} not found in shift snapshot."
                )
        else:
            if not eligible:
                raise NoEligibleStaffError(
                    f"No eligible staff for direct allocation of tip {tip.id}."
                )
            primary = eligible[0].staff
        allocation = allocate_direct(tip, primary)

    elif strategy_type == StrategyType.TIP_OUT:
        if not eligible:
            raise NoEligibleStaffError(
                f"No eligible staff for tip-out allocation of tip {tip.id}."
            )
        primary_sr = eligible[0]
        support = [
            (
                sr.staff,
                rule_set.role_weights.get(
                    sr.shift.active_role or sr.staff.roles[0], Decimal("1")
                ),
            )
            for sr in eligible[1:]
            if (sr.shift.active_role or sr.staff.roles[0]) in rule_set.support_roles
        ]
        allocation = allocate_tip_out(
            tip, primary_sr.staff, support, rule_set.tip_out_percentage
        )

    elif strategy_type == StrategyType.POOL:
        allocation = allocate_pool(tip, eligible, rule_set)

    elif strategy_type == StrategyType.MULTI_SERVICE:
        appt = strategy_kwargs.get("appointment") or appointment
        if appt is None:
            raise DistributionError(
                "Multi-service strategy requires an appointment but none was provided."
            )
        support = [
            (
                sr.staff,
                rule_set.role_weights.get(
                    sr.shift.active_role or sr.staff.roles[0], Decimal("1")
                ),
            )
            for sr in eligible
            if (sr.shift.active_role or sr.staff.roles[0]) in rule_set.support_roles
        ]
        allocation = allocate_multi_service(tip, appt, rule_set, support or None)

    else:
        raise DistributionError(f"Unhandled strategy type: {strategy_type}")

    per_staff = {}
    for entry in allocation.entries:
        per_staff[entry.staff_id] = per_staff.get(entry.staff_id, 0) + entry.amount_cents

    total_allocated = sum(per_staff.values())
    proof = ReconciliationProof(
        tip_total_cents=tip.amount_cents,
        allocation_total_cents=total_allocated,
        balanced=(total_allocated == tip.amount_cents),
    )
    if not proof.balanced:
        raise ReconciliationError(
            f"Reconciliation failure for tip {tip.id}: "
            f"expected {tip.amount_cents}¢, allocated {total_allocated}¢. "
            "This is a system bug, not a data problem."
        )

    return DistributionResult(
        tip_id=tip.id,
        rule_set_version_id=rule_set.id,
        shift_snapshot=shift_snapshot,
        per_staff_amounts=per_staff,
        strategy_used=strategy_type,
        rounding_method="largest_remainder",
        timestamp=datetime.utcnow(),
        reconciliation_proof=proof,
        cascade_record=allocation.cascade_record,
    )


def distribute_batch(
    tips_with_context: list[
        tuple[TipTransaction, list[ShiftRecord], Optional[Appointment], RuleSet]
    ],
) -> tuple[list[DistributionResult], BatchReconciliation]:
    """
    Allocates every tip in the batch, then runs batch-level reconciliation.

    All-or-nothing: if any single tip fails, the exception propagates immediately
    and no partial results are returned.
    """
    results: list[DistributionResult] = []
    for tip, snapshot, appointment, rule_set in tips_with_context:
        results.append(distribute_tip(tip, snapshot, rule_set, appointment))

    original_tips = [ctx[0] for ctx in tips_with_context]
    reconciliation = reconcile_batch(results, original_tips)
    return results, reconciliation


def reconcile_batch(
    results: list[DistributionResult],
    original_tips: list[TipTransaction],
) -> BatchReconciliation:
    """
    Verifies that Σ allocations == Σ tips across the batch.
    Raises ReconciliationError on any discrepancy (indicates a system bug).
    """
    total_tips = sum(t.amount_cents for t in original_tips)
    total_allocated = sum(sum(r.per_staff_amounts.values()) for r in results)
    balanced = total_tips == total_allocated

    if not balanced:
        raise ReconciliationError(
            f"Batch reconciliation failure: tips total={total_tips}¢, "
            f"allocated total={total_allocated}¢. "
            "This is a system bug — largest_remainder_round should prevent this."
        )

    return BatchReconciliation(
        tip_ids=[t.id for t in original_tips],
        total_tips_cents=total_tips,
        total_allocated_cents=total_allocated,
        balanced=balanced,
        per_tip_results=results,
    )


# ---------------------------------------------------------------------------
# Section 5 — Clawback
# ---------------------------------------------------------------------------

def compute_clawback(
    original_result: DistributionResult,
    charged_back_cents: int,
) -> ClawbackRecord:
    """
    Proportional reversal of a previously distributed allocation.
    Each recipient's clawback share mirrors their original allocation fraction.
    Rounding uses largest_remainder_round so the per-staff amounts sum exactly
    to charged_back_cents.
    """
    original_total = sum(original_result.per_staff_amounts.values())
    if original_total == 0:
        raise DistributionError(
            "Original distribution has zero total; cannot compute proportional clawback."
        )

    staff_ids = list(original_result.per_staff_amounts.keys())
    raw_clawbacks = [
        Decimal(original_result.per_staff_amounts[sid]) / Decimal(original_total)
        * Decimal(charged_back_cents)
        for sid in staff_ids
    ]
    rounded = largest_remainder_round(raw_clawbacks, charged_back_cents)

    return ClawbackRecord(
        original_distribution_id=original_result.id,
        charged_back_cents=charged_back_cents,
        per_staff_clawbacks=[
            ClawbackEntry(staff_id=sid, clawback_cents=cents)
            for sid, cents in zip(staff_ids, rounded)
        ],
        timestamp=datetime.utcnow(),
    )


def apply_clawback_to_balances(
    clawback: ClawbackRecord,
    current_balances: dict[str, int],
) -> tuple[list[BalanceAdjustment], dict[str, int]]:
    """
    Applies the clawback to each affected staff member's pending balance.

    Guarantees:
      - No adjusted balance goes negative.
      - Every clawback cent is either applied immediately or recorded as a residual.
      - Residuals are carried forward; nothing is silently lost.
    """
    adjustments: list[BalanceAdjustment] = []
    updated = dict(current_balances)

    for entry in clawback.per_staff_clawbacks:
        current = updated.get(entry.staff_id, 0)
        applied = min(current, entry.clawback_cents)
        residual = entry.clawback_cents - applied
        new_balance = current - applied

        adjustments.append(BalanceAdjustment(
            staff_id=entry.staff_id,
            previous_balance_cents=current,
            applied_clawback_cents=applied,
            new_balance_cents=new_balance,
            residual_cents=residual,
        ))
        updated[entry.staff_id] = new_balance

    return adjustments, updated
