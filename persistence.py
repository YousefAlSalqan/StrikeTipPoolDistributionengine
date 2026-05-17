"""
Persistence layer -- the two stores this system owns.

  RuleSetStore  -- versioned rule set history per salon
  AuditLedger   -- append-only record of every allocation and clawback

Both are implemented as in-memory dicts so the core engine can be exercised
without a database dependency.  Swap the internals for a real DB without
changing the public interface.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime
from typing import Optional

from models import (
    ClawbackRecord,
    DistributionResult,
    RuleSet,
)
from engine import (
    DistributionError,
    RuleSetNotFoundError,
    ValidationError,
    validate_rule_set,
)


# ---------------------------------------------------------------------------
# Rule Set Store
# ---------------------------------------------------------------------------

class RuleSetConflictError(DistributionError):
    """Two rule sets share the same effective date for the same salon."""


class RuleSetStore:
    """
    Versioned, append-only store of rule sets.

    Rule sets are never modified after saving.  Policy changes produce a new
    version with a later effective_date; old versions are preserved forever so
    that historical allocations remain reconstructable.
    """

    def __init__(self) -> None:
        self._store: dict[str, RuleSet] = {}   # rule_set.id -> RuleSet

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def save_rule_set(self, rule_set: RuleSet) -> RuleSet:
        """
        Validates and persists a new rule set version.

        Raises ValidationError if the rule set is internally inconsistent.
        Raises RuleSetConflictError if a version with the same effective date
        already exists for this salon -- the resolver cannot distinguish them.
        """
        validate_rule_set(rule_set)

        for existing in self._for_salon(rule_set.salon_id):
            if existing.effective_date == rule_set.effective_date:
                raise RuleSetConflictError(
                    f"Salon {rule_set.salon_id} already has a rule set with "
                    f"effective_date={rule_set.effective_date}. "
                    "Create a new version with a distinct effective date."
                )

        saved = deepcopy(rule_set)
        saved.id = str(uuid.uuid4())
        self._store[saved.id] = saved
        return saved

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_rule_set(self, rule_set_id: str) -> RuleSet:
        """
        Returns the rule set exactly as stored -- no migration, no field drift.
        Raises RuleSetNotFoundError if the ID is unknown.
        """
        rs = self._store.get(rule_set_id)
        if rs is None:
            raise RuleSetNotFoundError(f"Rule set '{rule_set_id}' not found.")
        return rs

    def resolve_active_rule_set(self, salon_id: str, timestamp: datetime) -> RuleSet:
        """
        Returns the rule set with the latest effective_date <= timestamp.

        This lookup is deterministic across time: the same inputs always return
        the same rule set, which is what makes historical reconstruction work.

        Raises RuleSetNotFoundError if no version exists at or before timestamp.
        """
        candidates = [
            rs for rs in self._for_salon(salon_id)
            if rs.effective_date <= timestamp
        ]
        if not candidates:
            raise RuleSetNotFoundError(
                f"No rule set found for salon '{salon_id}' at or before {timestamp}. "
                "Ensure a rule set has been saved with an effective_date <= tip timestamp."
            )
        return max(candidates, key=lambda rs: rs.effective_date)

    def list_rule_sets(self, salon_id: str) -> list[RuleSet]:
        """
        Chronological history of rule set versions for a salon.
        Complete -- no version is omitted -- supporting the regulatory requirement
        that staff can inspect the policy history.
        """
        return sorted(self._for_salon(salon_id), key=lambda rs: rs.effective_date)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _for_salon(self, salon_id: str) -> list[RuleSet]:
        return [rs for rs in self._store.values() if rs.salon_id == salon_id]


# ---------------------------------------------------------------------------
# Audit Ledger
# ---------------------------------------------------------------------------

class AuditLedger:
    """
    Append-only ledger of distribution and clawback records.

    Records are immutable once written.  Corrections are made by appending new
    records that reference the originals -- never by modifying existing entries.
    """

    def __init__(self) -> None:
        self._distributions: dict[str, DistributionResult] = {}
        self._clawbacks: dict[str, ClawbackRecord] = {}

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def append_distribution_record(self, result: DistributionResult) -> DistributionResult:
        """
        Durably stores one distribution result.

        Deep-copied on write so that caller mutations after the fact do not
        corrupt the ledger's record of what happened.
        Raises if a record with this ID already exists (idempotency guard).
        """
        if result.id is not None and result.id in self._distributions:
            raise DistributionError(
                f"Distribution record '{result.id}' already exists in the ledger."
            )
        stored = deepcopy(result)
        stored.id = stored.id or str(uuid.uuid4())
        self._distributions[stored.id] = stored
        return stored

    def append_clawback_record(self, clawback: ClawbackRecord) -> ClawbackRecord:
        """
        Durably stores a clawback as a new entry referencing the original allocation.

        The original distribution record is not modified -- the audit trail shows
        both the original allocation and its later reversal as separate entries.

        Raises if the referenced original distribution record does not exist.
        """
        if clawback.original_distribution_id not in self._distributions:
            raise DistributionError(
                f"Original distribution record '{clawback.original_distribution_id}' "
                "not found in ledger; cannot record clawback against it."
            )
        if clawback.id is not None and clawback.id in self._clawbacks:
            raise DistributionError(
                f"Clawback record '{clawback.id}' already exists in the ledger."
            )
        stored = deepcopy(clawback)
        stored.id = stored.id or str(uuid.uuid4())
        self._clawbacks[stored.id] = stored
        return stored

    # ------------------------------------------------------------------
    # Point reads
    # ------------------------------------------------------------------

    def get_distribution_record(self, record_id: str) -> DistributionResult:
        """Returns the record exactly as written. Raises if not found."""
        record = self._distributions.get(record_id)
        if record is None:
            raise DistributionError(
                f"Distribution record '{record_id}' not found in ledger."
            )
        return record

    # ------------------------------------------------------------------
    # Range queries
    # ------------------------------------------------------------------

    def get_worker_allocations(
        self,
        staff_id: str,
        period_start: datetime,
        period_end: datetime,
    ) -> dict:
        """
        Every distribution and clawback record affecting staff_id within
        [period_start, period_end].  Complete -- every relevant ledger entry
        is included.  Basis for staff transparency queries and dispute investigations.
        """
        distributions = [
            r for r in self._distributions.values()
            if staff_id in r.per_staff_amounts
            and period_start <= r.timestamp <= period_end
        ]
        clawbacks = [
            c for c in self._clawbacks.values()
            if any(e.staff_id == staff_id for e in c.per_staff_clawbacks)
            and period_start <= c.timestamp <= period_end
        ]
        return {
            "staff_id": staff_id,
            "period_start": period_start,
            "period_end": period_end,
            "distributions": distributions,
            "clawbacks": clawbacks,
        }

    # ------------------------------------------------------------------
    # Reconstruction
    # ------------------------------------------------------------------

    def reconstruct_allocation(
        self,
        record_id: str,
        rule_set_store: RuleSetStore,
    ) -> dict:
        """
        Produces a complete, human-readable account of how one allocation was computed.

        Self-contained: depends only on what was stored in the ledger and the rule
        set store at allocation time.  Does not query Strikepay's operational store.

        This is the function tribunals and auditors call -- its output is the
        system's regulatory defence when a staff member or owner disputes a figure.
        """
        record = self.get_distribution_record(record_id)
        rule_set = rule_set_store.get_rule_set(record.rule_set_version_id)

        eligible_staff = [
            sr for sr in record.shift_snapshot
            if sr.staff.id in record.per_staff_amounts
        ]

        lines: list[str] = [
            "=== Allocation Reconstruction ===",
            f"Record ID      : {record_id}",
            f"Tip ID         : {record.tip_id}",
            f"Timestamp      : {record.timestamp.isoformat()}",
            f"Strategy       : {record.strategy_used.value}",
            f"Rounding method: {record.rounding_method}",
            "",
            "--- Rule Set ---",
            f"ID             : {rule_set.id}",
            f"Salon          : {rule_set.salon_id}",
            f"Effective date : {rule_set.effective_date.isoformat()}",
            f"Tip-out %      : {rule_set.tip_out_percentage * 100:.2f}%",
            "",
            "--- Shift Snapshot (at time of allocation) ---",
        ]
        for sr in record.shift_snapshot:
            lines.append(
                f"  {sr.staff.name} ({sr.staff.id}) | "
                f"role={sr.shift.active_role or sr.staff.roles[0]} | "
                f"hours={sr.shift.hours_worked:.2f} | "
                f"comp={sr.staff.compensation_type.value}"
            )

        lines += ["", "--- Eligible Staff and Allocations ---"]
        for sr in eligible_staff:
            cents = record.per_staff_amounts[sr.staff.id]
            lines.append(f"  {sr.staff.name} ({sr.staff.id}): {cents}c (EUR {cents/100:.2f})")

        if record.cascade_record:
            cr = record.cascade_record
            lines += [
                "",
                "--- Tip-Out Cascade ---",
                f"  Original primary: {cr.original_primary_cents}c",
                f"  Tip-out %: {cr.tip_out_percentage * 100:.2f}%",
                f"  Reduced primary: {cr.reduced_primary_cents}c",
                f"  Total cascaded: {cr.total_cascaded_cents}c",
                "  Steps:",
            ]
            for step in cr.steps:
                lines.append(f"    {step.description} ({step.amount_cents}c)")

        proof = record.reconciliation_proof
        lines += [
            "",
            "--- Reconciliation ---",
            f"  Tip total  : {proof.tip_total_cents}c (EUR {proof.tip_total_cents/100:.2f})",
            f"  Allocated  : {proof.allocation_total_cents}c",
            f"  Balanced   : {proof.balanced}",
        ]

        return {
            "record_id": record_id,
            "tip_id": record.tip_id,
            "timestamp": record.timestamp,
            "rule_set": rule_set,
            "shift_snapshot": record.shift_snapshot,
            "eligible_staff": eligible_staff,
            "per_staff_amounts": record.per_staff_amounts,
            "cascade_record": record.cascade_record,
            "reconciliation_proof": record.reconciliation_proof,
            "narrative": "\n".join(lines),
        }
