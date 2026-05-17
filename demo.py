"""
Demo script -- runs every major scenario and prints readable output.

Scenarios:
  1.  Direct tip (booth renter)
  2.  Tip-out (stylist + assistant)
  3.  Pool distribution (four-person team)
  4.  Multi-service split (colour + cut)
  5.  Multi-service split with tip-out cascade
  6.  Batch processing + reconciliation
  7.  Chargeback -> clawback -> balance adjustment
  8.  Rule set versioning (mid-year policy change)
  9.  Eligibility edge cases
  10. Reconstruction audit trail
  11. Validation rejects bad rule set
"""

from decimal import Decimal
from datetime import datetime, timezone

from models import (
    Appointment, AppointmentContributor, CompensationType,
    RuleSet, Shift, ShiftRecord, StaffMember, StrategyType, TipTransaction,
)
from engine import (
    distribute_tip, distribute_batch, compute_clawback,
    apply_clawback_to_balances, validate_rule_set, ValidationError,
)
from persistence import AuditLedger, RuleSetStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEP  = "-" * 62
SEP2 = "=" * 62

def header(title):
    print(f"\n{SEP2}\n  {title}\n{SEP2}")

def section(title):
    print(f"\n{SEP}\n  {title}\n{SEP}")

def show_allocation(result):
    print(f"  Strategy  : {result.strategy_used.value}")
    for staff_id, cents in result.per_staff_amounts.items():
        name = ID_TO_NAME.get(staff_id, staff_id)
        print(f"  {name:<26} {cents:>6}c  (EUR {cents/100:.2f})")
    proof = result.reconciliation_proof
    tag = "[OK]" if proof.balanced else "[!!]"
    print(f"  Total     : {proof.allocation_total_cents}c  {tag}")
    if result.cascade_record and result.cascade_record.total_cascaded_cents > 0:
        cr = result.cascade_record
        print(f"  Cascade   : {cr.tip_out_percentage*100:.0f}% -> "
              f"{cr.total_cascaded_cents}c to support staff")

def money(cents):
    return f"{cents}c (EUR {cents/100:.2f})"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

tz = timezone.utc

ALICE  = StaffMember("s_alice",  "Alice (Lead Stylist)",  ["stylist"],      CompensationType.COMMISSION,  "senior")
BOB    = StaffMember("s_bob",    "Bob (Colourist)",        ["colourist"],    CompensationType.COMMISSION,  "mid")
CARA   = StaffMember("s_cara",   "Cara (Assistant)",       ["assistant"],    CompensationType.HOURLY,       "junior")
DAN    = StaffMember("s_dan",    "Dan (Receptionist)",     ["receptionist"], CompensationType.HOURLY,       "junior")
EVE    = StaffMember("s_eve",    "Eve (Booth Renter)",     ["stylist"],      CompensationType.BOOTH_RENTAL, "senior")
FRANK  = StaffMember("s_frank",  "Frank (Owner-Stylist)",  ["owner","stylist"], CompensationType.COMMISSION, "senior")

ID_TO_NAME = {s.id: s.name for s in [ALICE, BOB, CARA, DAN, EVE, FRANK]}

def day_shift(staff_id, salon_id, role):
    return Shift(
        id=f"sh_{staff_id}", staff_id=staff_id, salon_id=salon_id,
        clock_in=datetime(2026, 5, 17, 9, 0, tzinfo=tz),
        clock_out=datetime(2026, 5, 17, 18, 0, tzinfo=tz),
        active_role=role,
    )

TIP_TIME = datetime(2026, 5, 17, 14, 30, tzinfo=tz)

STORE  = RuleSetStore()
LEDGER = AuditLedger()

# ---------------------------------------------------------------------------
# Rule sets
# ---------------------------------------------------------------------------

RS_POOL = STORE.save_rule_set(RuleSet(
    salon_id="salon1",
    effective_date=datetime(2026, 1, 1, tzinfo=tz),
    default_strategy=StrategyType.POOL,
    role_weights={
        "stylist":      Decimal("1.0"),
        "colourist":    Decimal("1.0"),
        "assistant":    Decimal("0.4"),
        "receptionist": Decimal("0.2"),
    },
    seniority_multipliers={"senior": Decimal("1.2"), "mid": Decimal("1.0"), "junior": Decimal("1.0")},
    tip_out_percentage=Decimal("0.10"),
    support_roles=["assistant", "receptionist"],
    eligibility_by_compensation={"commission": True, "hourly": True, "booth_rental": False},
    multi_service_for_multi_provider=True,
))

RS_TIPOUT = STORE.save_rule_set(RuleSet(
    salon_id="salon1",
    effective_date=datetime(2026, 4, 1, tzinfo=tz),
    default_strategy=StrategyType.TIP_OUT,
    role_weights={
        "stylist":      Decimal("1.0"),
        "colourist":    Decimal("1.0"),
        "assistant":    Decimal("0.5"),
        "receptionist": Decimal("0.2"),
    },
    seniority_multipliers={"senior": Decimal("1.2"), "mid": Decimal("1.0"), "junior": Decimal("1.0")},
    tip_out_percentage=Decimal("0.12"),
    support_roles=["assistant", "receptionist"],
    eligibility_by_compensation={"commission": True, "hourly": True, "booth_rental": False},
    multi_service_for_multi_provider=True,
))

print(f"Rule set V1 (pool)    saved: {RS_POOL.id[:8]}...  effective {RS_POOL.effective_date.date()}")
print(f"Rule set V2 (tip-out) saved: {RS_TIPOUT.id[:8]}...  effective {RS_TIPOUT.effective_date.date()}")


# ---------------------------------------------------------------------------
# Scenario 1 -- Direct tip (booth renter keeps the whole amount)
# ---------------------------------------------------------------------------
header("Scenario 1 -- Direct Tip (Booth Renter)")

tip1 = TipTransaction("t1", 2500, "EUR", TIP_TIME, "card", "salon1", linked_staff_id=EVE.id)
snapshot1 = [ShiftRecord(EVE, day_shift(EVE.id, "salon1", "stylist"))]

result1 = distribute_tip(tip1, snapshot1, RS_POOL)
print(f"\n  Tip: {money(tip1.amount_cents)}  directed to {EVE.name}")
print(f"  (Booth renters excluded from pool; linked_staff_id forces DIRECT strategy)")
show_allocation(result1)


# ---------------------------------------------------------------------------
# Scenario 2 -- Tip-out: stylist 88%, assistant 12%
# ---------------------------------------------------------------------------
header("Scenario 2 -- Tip-Out (Stylist + Assistant, 12% cascade)")

tip2 = TipTransaction("t2", 3000, "EUR", TIP_TIME, "card", "salon1")
snapshot2 = [
    ShiftRecord(ALICE, day_shift(ALICE.id, "salon1", "stylist")),
    ShiftRecord(CARA,  day_shift(CARA.id,  "salon1", "assistant")),
]

result2 = distribute_tip(tip2, snapshot2, RS_TIPOUT)
print(f"\n  Tip: {money(tip2.amount_cents)}")
print(f"  Rule set V2: tip-out @ 12%")
print(f"  Expected: Alice 2640c (88%), Cara 360c (12%)")
show_allocation(result2)


# ---------------------------------------------------------------------------
# Scenario 3 -- Pool: four-person team, weighted by role + seniority + hours
# ---------------------------------------------------------------------------
header("Scenario 3 -- Pool Distribution (Four-Person Team, EUR 20 tip)")

tip3 = TipTransaction("t3", 2000, "EUR", TIP_TIME, "card", "salon1")
snapshot3 = [
    ShiftRecord(ALICE, day_shift(ALICE.id, "salon1", "stylist")),
    ShiftRecord(BOB,   day_shift(BOB.id,   "salon1", "colourist")),
    ShiftRecord(CARA,  day_shift(CARA.id,  "salon1", "assistant")),
    ShiftRecord(DAN,   day_shift(DAN.id,   "salon1", "receptionist")),
]

result3 = distribute_tip(tip3, snapshot3, RS_POOL)
print(f"\n  Tip: {money(tip3.amount_cents)}")
print(f"  Weights: stylist x1.0, colourist x1.0, assistant x0.4, receptionist x0.2")
print(f"  Seniority bonus: Alice x1.2 (senior), others x1.0")
print(f"  Total weight: Alice=1.2, Bob=1.0, Cara=0.4, Dan=0.2  -> sum=2.8")
show_allocation(result3)


# ---------------------------------------------------------------------------
# Scenario 4 -- Multi-service: colour EUR 100 + cut EUR 50, tip EUR 30
# ---------------------------------------------------------------------------
header("Scenario 4 -- Multi-Service Split (Colour + Cut)")

appt4 = Appointment(
    id="appt4", salon_id="salon1",
    contributors=[
        AppointmentContributor(staff_id=BOB.id,   role="colourist", service_value_cents=10000),
        AppointmentContributor(staff_id=ALICE.id, role="stylist",   service_value_cents=5000),
    ],
    start_time=datetime(2026, 5, 17, 10, 0, tzinfo=tz),
    end_time=datetime(2026, 5, 17, 13, 0, tzinfo=tz),
)

tip4 = TipTransaction("t4", 3000, "EUR", TIP_TIME, "card", "salon1", linked_appointment_id="appt4")
snapshot4 = [
    ShiftRecord(BOB,   day_shift(BOB.id,   "salon1", "colourist")),
    ShiftRecord(ALICE, day_shift(ALICE.id, "salon1", "stylist")),
]

result4 = distribute_tip(tip4, snapshot4, RS_POOL, appointment=appt4)
print(f"\n  Appointment: Colour EUR 100 (Bob) + Cut EUR 50 (Alice) = EUR 150 total")
print(f"  Tip: {money(tip4.amount_cents)}")
print(f"  Expected: Bob 2000c (2/3), Alice 1000c (1/3)")
show_allocation(result4)


# ---------------------------------------------------------------------------
# Scenario 5 -- Multi-service + tip-out cascade
# ---------------------------------------------------------------------------
header("Scenario 5 -- Multi-Service Split WITH Tip-Out Cascade")

appt5 = Appointment(
    id="appt5", salon_id="salon1",
    contributors=[
        AppointmentContributor(staff_id=BOB.id,   role="colourist", service_value_cents=10000),
        AppointmentContributor(staff_id=ALICE.id, role="stylist",   service_value_cents=5000),
    ],
    start_time=datetime(2026, 5, 17, 10, 0, tzinfo=tz),
    end_time=datetime(2026, 5, 17, 13, 0, tzinfo=tz),
)

tip5 = TipTransaction("t5", 3000, "EUR", TIP_TIME, "card", "salon1", linked_appointment_id="appt5")
snapshot5 = [
    ShiftRecord(BOB,   day_shift(BOB.id,   "salon1", "colourist")),
    ShiftRecord(ALICE, day_shift(ALICE.id, "salon1", "stylist")),
    ShiftRecord(CARA,  day_shift(CARA.id,  "salon1", "assistant")),
]

result5 = distribute_tip(tip5, snapshot5, RS_TIPOUT, appointment=appt5)
print(f"\n  Same appointment as Scenario 4, but rule set V2 (tip-out 12%, Cara on shift)")
show_allocation(result5)


# ---------------------------------------------------------------------------
# Scenario 6 -- Batch: three tips, one reconciliation
# ---------------------------------------------------------------------------
header("Scenario 6 -- Batch Processing + Reconciliation")

tips_batch = [
    TipTransaction("b1", 1500, "EUR", TIP_TIME, "qr",   "salon1"),
    TipTransaction("b2", 2250, "EUR", TIP_TIME, "card", "salon1"),
    TipTransaction("b3",  750, "EUR", TIP_TIME, "nfc",  "salon1"),
]
snapshot_batch = [
    ShiftRecord(ALICE, day_shift(ALICE.id, "salon1", "stylist")),
    ShiftRecord(BOB,   day_shift(BOB.id,   "salon1", "colourist")),
    ShiftRecord(CARA,  day_shift(CARA.id,  "salon1", "assistant")),
]

batch_results, batch_recon = distribute_batch(
    [(t, snapshot_batch, None, RS_POOL) for t in tips_batch]
)

print(f"\n  Tips in batch    : {len(tips_batch)}")
print(f"  Total tips       : {money(batch_recon.total_tips_cents)}")
print(f"  Total allocated  : {money(batch_recon.total_allocated_cents)}")
print(f"  Balanced         : {'[OK]' if batch_recon.balanced else '[!!]'}")
print()
for tip, res in zip(tips_batch, batch_results):
    parts = ", ".join(
        f"{ID_TO_NAME[sid].split()[0]} {c}c"
        for sid, c in res.per_staff_amounts.items()
    )
    print(f"  Tip {tip.id} ({money(tip.amount_cents)}): {parts}")


# ---------------------------------------------------------------------------
# Scenario 7 -- Chargeback -> clawback -> balance adjustment
# ---------------------------------------------------------------------------
header("Scenario 7 -- Chargeback & Clawback")

original = LEDGER.append_distribution_record(result3)

print(f"\n  Original allocation (Scenario 3, EUR 20 pool):")
for sid, cents in original.per_staff_amounts.items():
    print(f"    {ID_TO_NAME[sid]:<28}  received {cents}c")

charged_back = 1000
print(f"\n  Client charges back: {money(charged_back)}")

clawback = compute_clawback(original, charged_back)
print(f"\n  Proportional clawback (mirrors original split):")
for e in clawback.per_staff_clawbacks:
    print(f"    {ID_TO_NAME[e.staff_id]:<28}  owes {e.clawback_cents}c")

balances = {ALICE.id: 600, BOB.id: 400, CARA.id: 80, DAN.id: 20}
print(f"\n  Pending balances before clawback:")
for sid, bal in balances.items():
    print(f"    {ID_TO_NAME[sid]:<28}  {bal}c")

adjustments, updated = apply_clawback_to_balances(clawback, balances)
print(f"\n  Balances after clawback:")
for adj in adjustments:
    name = ID_TO_NAME[adj.staff_id]
    residual = f"  (residual {adj.residual_cents}c carried forward)" if adj.residual_cents else ""
    print(f"    {name:<28}  {adj.previous_balance_cents}c -> {adj.new_balance_cents}c{residual}")


# ---------------------------------------------------------------------------
# Scenario 8 -- Rule set versioning
# ---------------------------------------------------------------------------
header("Scenario 8 -- Rule Set Version Resolution (Policy Change Mid-Year)")

march_ts = datetime(2026, 3, 15, 14, 0, tzinfo=tz)
may_ts   = datetime(2026, 5,  1, 14, 0, tzinfo=tz)

rs_march = STORE.resolve_active_rule_set("salon1", march_ts)
rs_may   = STORE.resolve_active_rule_set("salon1", may_ts)

print(f"\n  Tip on {march_ts.date()} -> rule set effective {rs_march.effective_date.date()}"
      f"  (strategy: {rs_march.default_strategy.value})")
print(f"  Tip on {may_ts.date()}   -> rule set effective {rs_may.effective_date.date()}"
      f"  (strategy: {rs_may.default_strategy.value})")

print(f"\n  Full version history for salon1:")
for rs in STORE.list_rule_sets("salon1"):
    print(f"    [{rs.effective_date.date()}]  {rs.id[:8]}...  strategy={rs.default_strategy.value}")


# ---------------------------------------------------------------------------
# Scenario 9 -- Eligibility edge cases
# ---------------------------------------------------------------------------
header("Scenario 9 -- Eligibility Edge Cases")

section("9a -- Booth renter excluded from commission pool")
tip9a = TipTransaction("t9a", 1000, "EUR", TIP_TIME, "card", "salon1")
snapshot9a = [
    ShiftRecord(ALICE, day_shift(ALICE.id, "salon1", "stylist")),
    ShiftRecord(EVE,   day_shift(EVE.id,   "salon1", "stylist")),
]
result9a = distribute_tip(tip9a, snapshot9a, RS_POOL)
print(f"\n  Alice (commission) + Eve (booth renter) both on shift")
show_allocation(result9a)
print(f"  -> Eve receives 0c: booth_rental excluded in eligibility_by_compensation")

section("9b -- Owner-operator clocked in as stylist -> eligible")
tip9b = TipTransaction("t9b", 1000, "EUR", TIP_TIME, "card", "salon1")
frank_shift = Shift(
    id="sh_frank", staff_id=FRANK.id, salon_id="salon1",
    clock_in=datetime(2026, 5, 17, 9, 0, tzinfo=tz),
    clock_out=datetime(2026, 5, 17, 18, 0, tzinfo=tz),
    active_role="stylist",
)
snapshot9b = [
    ShiftRecord(ALICE, day_shift(ALICE.id, "salon1", "stylist")),
    ShiftRecord(FRANK, frank_shift),
]
result9b = distribute_tip(tip9b, snapshot9b, RS_POOL)
print(f"\n  Alice + Frank (owner, active_role=stylist)")
show_allocation(result9b)
print(f"  -> Frank participates: shift declares active_role=stylist")

section("9c -- Staff not on shift at tip time -> excluded")
tip9c = TipTransaction("t9c", 1000, "EUR", TIP_TIME, "card", "salon1")
late_shift = Shift(
    id="sh_cara_late", staff_id=CARA.id, salon_id="salon1",
    clock_in=datetime(2026, 5, 17, 15, 0, tzinfo=tz),   # clocks in at 15:00
    clock_out=datetime(2026, 5, 17, 20, 0, tzinfo=tz),
    active_role="assistant",
)
snapshot9c = [
    ShiftRecord(ALICE, day_shift(ALICE.id, "salon1", "stylist")),
    ShiftRecord(CARA,  late_shift),
]
result9c = distribute_tip(tip9c, snapshot9c, RS_POOL)
print(f"\n  Tip at 14:30 | Cara's shift starts at 15:00")
show_allocation(result9c)
print(f"  -> Cara excluded: tip timestamp precedes her clock-in")


# ---------------------------------------------------------------------------
# Scenario 10 -- Full audit reconstruction
# ---------------------------------------------------------------------------
header("Scenario 10 -- Audit Trail Reconstruction")

stored4 = LEDGER.append_distribution_record(result4)
recon = LEDGER.reconstruct_allocation(stored4.id, STORE)
print()
print(recon["narrative"])


# ---------------------------------------------------------------------------
# Scenario 11 -- Validation rejects a bad rule set (all errors at once)
# ---------------------------------------------------------------------------
header("Scenario 11 -- Rule Set Validation (All Errors Reported Together)")

bad = RuleSet(
    salon_id="salon1",
    effective_date=None,
    default_strategy=StrategyType.POOL,
    role_weights={"stylist": Decimal("-0.5")},
    seniority_multipliers={"senior": Decimal("-1")},
    tip_out_percentage=Decimal("1.5"),
    support_roles=[],
    eligibility_by_compensation={"ghost_type": True},
)

print()
try:
    validate_rule_set(bad)
except ValidationError as e:
    print(f"  ValidationError: {len(e.errors)} defects found")
    for err in e.errors:
        print(f"    * {err}")

print(f"\n{SEP2}")
print("  All scenarios complete.")
print(SEP2)
