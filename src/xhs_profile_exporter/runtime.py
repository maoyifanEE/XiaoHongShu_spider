from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .state import LoginStatus


class SafeStopRequested(Exception):
    def __init__(self, status: LoginStatus, reason: str, phase: str, note_id: str | None = None):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.phase = phase
        self.note_id = note_id


@dataclass
class OpenNoteResult:
    page: Any
    note_id: str
    strategy: str
    target_verified: bool
    detail_kind: str | None = None
    reason: str | None = None


@dataclass
class VisibleCardProbe:
    note_id: str
    discovered_index: int
    card_visible: bool
    locator_kind: str
    dom_summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class NavigationProbeResult:
    note_id: str
    discovered_index: int
    strategy: str
    card_visible: bool
    locator_kind: str
    click_target_kind: str
    url_changed: bool
    new_page_created: bool
    dialog_before: int
    dialog_after: int
    detail_root_before: int
    detail_root_after: int
    target_verified: bool
    elapsed_ms: int
    failure_reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class NavigationExperimentResult:
    creator_name: str
    creator_id: str
    status: str
    candidates_seen: int = 0
    attempts: list[NavigationProbeResult] = field(default_factory=list)
    safe_stop_reason: str | None = None
    confirmed_interaction_model: str | None = None
    reliable_strategy: str | None = None

    @property
    def success_count(self) -> int:
        return sum(1 for item in self.attempts if item.target_verified)


@dataclass
class CollectionResult:
    attempted_ids: list[str] = field(default_factory=list)
    verified_ids: list[str] = field(default_factory=list)
    exportable_ids: list[str] = field(default_factory=list)
    non_exportable_ids: list[str] = field(default_factory=list)
    navigation_failed_ids: list[str] = field(default_factory=list)
    non_public_ids: list[str] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)
    safe_stop_status: LoginStatus | None = None
    safe_stop_reason: str | None = None
    navigation_strategy_counts: dict[str, int] = field(default_factory=dict)
    profile_return_counts: dict[str, int] = field(default_factory=dict)
    field_presence: dict[str, dict[str, int]] = field(default_factory=dict)
    field_source_counts: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def attempted_count(self) -> int:
        return len(self.attempted_ids)

    @property
    def unresolved_count(self) -> int:
        return len(self.navigation_failed_ids) + len(self.failed_ids)

    def count_navigation_strategy(self, strategy: str | None) -> None:
        key = strategy or "UNKNOWN"
        self.navigation_strategy_counts[key] = self.navigation_strategy_counts.get(key, 0) + 1

    def count_profile_return(self, strategy: str | None) -> None:
        key = strategy or "UNKNOWN"
        self.profile_return_counts[key] = self.profile_return_counts.get(key, 0) + 1

    def record_field(self, field_name: str, present: bool, source: str | None = None) -> None:
        bucket = self.field_presence.setdefault(field_name, {"present": 0, "missing": 0})
        bucket["present" if present else "missing"] += 1
        source_key = source if present and source else "MISSING"
        sources = self.field_source_counts.setdefault(field_name, {})
        sources[source_key] = sources.get(source_key, 0) + 1


class RunBudget:
    def __init__(self, max_page_visits: int | None = None, max_runtime_minutes: float | None = None):
        self.max_page_visits = max_page_visits
        self.page_visits = 0
        self.started_monotonic = time.monotonic()
        self.deadline = None if max_runtime_minutes is None else self.started_monotonic + max_runtime_minutes * 60

    def check(self, phase: str, note_id: str | None = None) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise SafeStopRequested(LoginStatus.PAGE_NOT_READY, "RUNTIME_LIMIT", phase, note_id)
        if self.max_page_visits is not None and self.page_visits >= self.max_page_visits:
            raise SafeStopRequested(LoginStatus.PAGE_NOT_READY, "PAGE_VISIT_LIMIT", phase, note_id)

    def count_page_visit(self, phase: str, note_id: str | None = None) -> None:
        self.check(phase, note_id)
        self.page_visits += 1
