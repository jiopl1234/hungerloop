"""Hunger drive engine for HungerLoop v0.4.1.

:class:`HungerEngine` computes the per-loop hunger snapshot: drive budget after
decay, work pressure from the ledger, phase via hysteresis, and stop reason.

**Stop-reason priority** (encodes I-9 — BLOCKED before DONE):
    1. ``clock.frozen`` → ``HUMAN_PAUSED``
    2. cost or token ceiling → ``SAFETY_STOP``
    3. all remaining items BLOCKED → ``BLOCKED``
    4. ``drive_budget <= 0`` and not done → ``HUNGER_EXPIRED``
    5. ledger is done → ``DONE``

Decay types: ``LINEAR`` (wall-clock), ``LOOP_COUNT`` (loop iterations),
``STAGE_BASED`` (wall-clock with future stage hooks; MVP treats it as linear).
"""
from __future__ import annotations

from datetime import datetime, timezone

from hungerloop.models.enums import DecayType, LoopPhase, StopReason
from hungerloop.models.hunger import (
    HungerClockState,
    HungerLedger,
    HungerPolicy,
    HungerSnapshot,
)


class HungerEngine:
    """Compute the per-loop hunger snapshot."""

    def tick(
        self,
        policy: HungerPolicy,
        clock: HungerClockState,
        ledger: HungerLedger,
        previous_phase: LoopPhase | None = None,
        now: datetime | None = None,
    ) -> HungerSnapshot:
        """Compute one tick's worth of hunger state.

        Args:
            policy: Hunger decay policy.
            clock: Current clock state (loop count, frozen, consumed).
            ledger: Hunger ledger of items.
            previous_phase: Phase from previous tick (for hysteresis).
            now: Optional timestamp; defaults to UTC now.

        Returns:
            A :class:`HungerSnapshot` with drive budget, phase, and stop info.
        """
        now = now or datetime.now(timezone.utc)

        drive_budget = self._compute_drive_budget(policy, clock, now)
        drive_budget = max(0.0, min(policy.h_max, drive_budget))

        work_pressure = ledger.work_pressure() * policy.h_max
        active_hunger = min(drive_budget, work_pressure)
        drive_ratio = drive_budget / policy.h_max if policy.h_max > 0 else 0.0
        phase = self._phase_with_hysteresis(drive_ratio, previous_phase)

        should_stop = False
        stop_reason: StopReason | None = None

        if clock.frozen:
            should_stop = True
            stop_reason = StopReason.HUMAN_PAUSED

        elif clock.consumed_by_cost_usd >= policy.max_total_cost_usd:
            should_stop = True
            stop_reason = StopReason.SAFETY_STOP

        elif clock.consumed_tokens >= policy.max_total_tokens:
            should_stop = True
            stop_reason = StopReason.SAFETY_STOP

        elif ledger.all_remaining_items_blocked():
            should_stop = True
            stop_reason = StopReason.BLOCKED

        elif drive_budget <= 0 and not ledger.is_done():
            should_stop = True
            stop_reason = StopReason.HUNGER_EXPIRED

        elif ledger.is_done():
            should_stop = True
            stop_reason = StopReason.DONE

        return HungerSnapshot(
            drive_budget=drive_budget,
            work_pressure=work_pressure,
            active_hunger=active_hunger,
            drive_ratio=drive_ratio,
            phase=phase,
            should_stop=should_stop,
            stop_reason=stop_reason,
        )

    def _compute_drive_budget(
        self,
        policy: HungerPolicy,
        clock: HungerClockState,
        now: datetime,
    ) -> float:
        """Compute the raw drive budget before clamping.

        ``LOOP_COUNT`` decay is evaluated before the ``started_at is None``
        short-circuit because it does not depend on wall-clock time — only on
        ``clock.loop_count``. Wall-clock decays (``LINEAR``, ``STAGE_BASED``)
        require ``started_at`` to compute elapsed time.
        """
        if clock.manually_cleared:
            return 0.0

        # LOOP_COUNT decay does not need wall-clock or started_at.
        if policy.decay_type == DecayType.LOOP_COUNT:
            max_loops = int(policy.decay_duration_seconds)
            if max_loops <= 0:
                return policy.initial_hunger
            remaining = max(0, max_loops - clock.loop_count)
            return policy.initial_hunger * (remaining / max_loops)

        # Wall-clock decays need started_at.
        if policy.started_at is None:
            return policy.initial_hunger

        if policy.decay_type == DecayType.LINEAR:
            elapsed = max(0.0, (now - policy.started_at).total_seconds())
            ratio = min(1.0, elapsed / policy.decay_duration_seconds)
            return policy.initial_hunger * (1.0 - ratio)

        if policy.decay_type == DecayType.STAGE_BASED:
            elapsed = max(0.0, (now - policy.started_at).total_seconds())
            return self._compute_stage_budget(policy, elapsed)

        raise NotImplementedError(f"{policy.decay_type} not in MVP")

    def _compute_stage_budget(
        self, policy: HungerPolicy, elapsed: float
    ) -> float:
        """MVP stage-based decay: behaves like LINEAR."""
        total = policy.decay_duration_seconds
        if elapsed >= total:
            return 0.0
        ratio = elapsed / total
        return policy.initial_hunger * (1.0 - ratio)

    def _phase_with_hysteresis(
        self, drive_ratio: float, previous: LoopPhase | None
    ) -> LoopPhase:
        """Decide phase with hysteresis to prevent flapping at boundaries.

        - ratio > 0.6 → EXPLORE
        - 0.3 < ratio <= 0.6 → EXPLORE if previous was EXPLORE, else EXPLOIT
        - ratio <= 0.3 → COOLDOWN
        """
        if drive_ratio > 0.6:
            return LoopPhase.EXPLORE
        if drive_ratio > 0.3:
            if previous == LoopPhase.EXPLORE:
                return LoopPhase.EXPLORE
            return LoopPhase.EXPLOIT
        return LoopPhase.COOLDOWN
