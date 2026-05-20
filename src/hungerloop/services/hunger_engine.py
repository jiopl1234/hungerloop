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

from dataclasses import dataclass
from datetime import datetime, timezone

from hungerloop.models.enums import DecayType, LoopPhase, StopReason, ValidationVerdict
from hungerloop.models.events import EventType
from hungerloop.models.hunger import (
    HungerClockState,
    HungerLedger,
    HungerPolicy,
    HungerSnapshot,
)
from hungerloop.models.mission import MissionPhase, MissionPhaseStatus
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.validation_pipeline import (
    ValidationPipelineResult,
    ValidationPipelineVerdict,
)


@dataclass(frozen=True)
class _PhaseValidationOutcome:
    pipeline_verdict: ValidationPipelineVerdict
    deterministic_regressed: bool
    scrutiny_verdict: ValidationVerdict | None
    user_testing_verdict: ValidationVerdict | None


class HungerEngine:
    """Compute the per-loop hunger snapshot."""

    def __init__(self, repo: RepositoryProtocol | None = None) -> None:
        self.repo = repo

    def tick(
        self,
        policy: HungerPolicy,
        clock: HungerClockState,
        ledger: HungerLedger,
        previous_phase: LoopPhase | None = None,
        now: datetime | None = None,
        *,
        task_id: str | None = None,
        validation_result: ValidationPipelineResult | None = None,
        validation_phase_id: str | None = None,
    ) -> HungerSnapshot:
        """Compute one tick's worth of hunger state.

        Args:
            policy: Hunger decay policy.
            clock: Current clock state (loop count, frozen, consumed).
            ledger: Hunger ledger of items.
            previous_phase: Phase from previous tick (for hysteresis).
            now: Optional timestamp; defaults to UTC now.
            task_id: Optional task identifier; when provided with a repository,
                mission phase-state transitions are evaluated as part of the tick.
            validation_result: Optional most recent validation-pipeline result for
                a ``validating`` phase.
            validation_phase_id: The phase the validation result belongs to.

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

        ledger_done = ledger.is_done()
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

        elif drive_budget <= 0 and not ledger_done:
            should_stop = True
            stop_reason = StopReason.HUNGER_EXPIRED

        elif ledger_done:
            should_stop = True
            stop_reason = StopReason.DONE

        if task_id is not None and self.repo is not None:
            phase_transition_status = self._advance_mission_phases(
                task_id=task_id,
                validation_result=validation_result,
                validation_phase_id=validation_phase_id,
                now=now,
            )
            if (
                stop_reason == StopReason.DONE
                and phase_transition_status == "validating"
            ):
                should_stop = False
                stop_reason = None
            elif stop_reason == StopReason.DONE and self._mission_blocks_done_stop(
                task_id
            ):
                should_stop = False
                stop_reason = None

        return HungerSnapshot(
            drive_budget=drive_budget,
            work_pressure=work_pressure,
            active_hunger=active_hunger,
            drive_ratio=drive_ratio,
            phase=phase,
            should_stop=should_stop,
            stop_reason=stop_reason,
        )

    def _mission_blocks_done_stop(self, task_id: str | None) -> bool:
        """Return True when a mission task still has unfinished phase state."""
        if task_id is None or self.repo is None:
            return False
        mission = self.repo.get_mission(task_id)
        if mission is None:
            return False
        return any(phase.status != "done" for phase in mission.phases)

    def _advance_mission_phases(
        self,
        *,
        task_id: str,
        validation_result: ValidationPipelineResult | None,
        validation_phase_id: str | None,
        now: datetime,
    ) -> MissionPhaseStatus | None:
        """Apply the v0.6 mission phase state machine.

        ``HungerEngine.tick()`` is the sole writer of ``mission_phases.status``.
        The repository enforces illegal terminal edges such as ``done -> *``.
        """
        assert self.repo is not None
        mission = self.repo.get_mission(task_id)
        if mission is None:
            return None

        last_transition_status: MissionPhaseStatus | None = None
        for phase in mission.phases:
            validation_outcome = self._phase_validation_outcome(
                validation_result,
                validation_phase_id,
                phase,
            )
            if phase.status == "done":
                continue
            if self._should_start_phase(phase):
                last_transition_status = "in_progress"
                self._transition_phase(
                    task_id=task_id,
                    mission_id=mission.mission_id,
                    phase=phase,
                    status="in_progress",
                    event_type=EventType.MISSION_PHASE_STARTED,
                    completed_at=None,
                )
                continue
            if self._should_start_validation(phase):
                last_transition_status = "validating"
                self._transition_phase(
                    task_id=task_id,
                    mission_id=mission.mission_id,
                    phase=phase,
                    status="validating",
                    event_type=EventType.MISSION_PHASE_VALIDATION_STARTED,
                    completed_at=None,
                )
                continue
            if phase.status == "validating":
                if self._validation_failed(validation_outcome):
                    last_transition_status = "in_progress"
                    self._transition_phase(
                        task_id=task_id,
                        mission_id=mission.mission_id,
                        phase=phase,
                        status="in_progress",
                        event_type=EventType.MISSION_PHASE_VALIDATION_FAILED,
                        completed_at=None,
                    )
                elif self._validation_passed(validation_outcome):
                    last_transition_status = "done"
                    self._transition_phase(
                        task_id=task_id,
                        mission_id=mission.mission_id,
                        phase=phase,
                        status="done",
                        event_type=EventType.MISSION_PHASE_COMPLETED,
                        completed_at=now,
                    )
        return last_transition_status

    def _should_start_phase(self, phase: MissionPhase) -> bool:
        """Return True when a pending phase has its first feature in progress."""
        if phase.status != "pending":
            return False
        assert self.repo is not None
        return any(
            feature.status == "in_progress"
            for feature in self.repo.list_features_for_phase(phase.phase_id)
        )

    def _should_start_validation(self, phase: MissionPhase) -> bool:
        """Return True when all features in an in-progress phase are done."""
        if phase.status != "in_progress":
            return False
        assert self.repo is not None
        features = self.repo.list_features_for_phase(phase.phase_id)
        return bool(features) and all(feature.status == "done" for feature in features)

    @staticmethod
    def _phase_validation_outcome(
        validation_result: ValidationPipelineResult | None,
        validation_phase_id: str | None,
        phase: MissionPhase,
    ) -> _PhaseValidationOutcome | None:
        if validation_result is None or validation_phase_id != phase.phase_id:
            return None
        return _PhaseValidationOutcome(
            pipeline_verdict=validation_result.pipeline_verdict,
            deterministic_regressed=bool(
                validation_result.deterministic_report.regressed_check_keys
            ),
            scrutiny_verdict=(
                validation_result.scrutiny_report.verdict
                if validation_result.scrutiny_report is not None
                else None
            ),
            user_testing_verdict=(
                validation_result.user_testing_report.verdict
                if validation_result.user_testing_report is not None
                else None
            ),
        )

    @staticmethod
    def _validation_failed(outcome: _PhaseValidationOutcome | None) -> bool:
        if outcome is None:
            return False
        return outcome.pipeline_verdict == "fail"

    @staticmethod
    def _validation_passed(outcome: _PhaseValidationOutcome | None) -> bool:
        if outcome is None:
            return False
        if outcome.pipeline_verdict != "pass":
            return False
        if outcome.deterministic_regressed:
            return False
        if outcome.scrutiny_verdict is None:
            return False
        if outcome.user_testing_verdict is None:
            return False
        return outcome.scrutiny_verdict == ValidationVerdict.PASS and (
            outcome.user_testing_verdict == ValidationVerdict.PASS
        )

    def _transition_phase(
        self,
        *,
        task_id: str,
        mission_id: str,
        phase: MissionPhase,
        status: MissionPhaseStatus,
        event_type: EventType,
        completed_at: datetime | None,
    ) -> None:
        assert self.repo is not None
        previous_status = phase.status
        self.repo.update_phase_status(
            phase.phase_id,
            status,
            completed_at=completed_at,
        )
        self.repo.append_event(
            event_type,
            {
                "mission_id": mission_id,
                "phase_id": phase.phase_id,
                "previous_status": previous_status,
                "new_status": status,
            },
            task_id=task_id,
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
