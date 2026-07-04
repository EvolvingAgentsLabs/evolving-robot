"""Evolution controller: agentvcs as the robot's genetic memory.

The robot versions its own skills with agentvcs. Each evolution step:

  1. commit the current skills as the baseline,
  2. dream -> rewrite a skill (already gated by skill-map),
  3. commit the evolved skills,
  4. re-score with a mission,
  5. if the score regressed, ``rollback(reason=...)`` (restoring the skill files and
     recording *why* in the durable ledger); otherwise keep it,
  6. when a skill set is verified good, ``freeze`` (crystallize) it.

The agentvcs operations are real; a caller supplies ``score_fn`` (a live mission run, or an
injected score for a deterministic demo).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from agentvcs import Repository, crystallize
from agentvcs.repository import RepoError

DEFAULT_MANIFEST = {
    "goal": "Patrol the facility: visit every checkpoint and report it clear.",
    "models": [{"provider": "google", "model": "gemma-4-26b-a4b-it"}],
    "mode": "vcs",
    # Version the odyssey mission trace alongside code+goal. The db path resolves
    # against the repo workdir (robot_brain/skills) -> the project-level .odyssey db.
    # Uses agentvcs's `odyssey` trace provider (dogfooded in Phase 6).
    "trace": {"provider": "odyssey", "db": "../../.odyssey/missions.db"},
}


@dataclass
class StepResult:
    evolved: bool
    kept: bool = False
    rolled_back: bool = False
    baseline_score: float = 0.0
    new_score: float = 0.0
    commit: Optional[str] = None
    reason: str = ""


class EvolutionController:
    def __init__(self, skills_dir: Path | str, manifest: Optional[dict] = None):
        self.skills_dir = Path(skills_dir)
        # Open ONLY a repo rooted at skills_dir. Repository.open() walks up to
        # ancestor .agentvcs dirs, which would silently bind to a workspace-level
        # repo; guard against that so we always version the skills tree itself.
        if (self.skills_dir / ".agentvcs").exists():
            self.repo = Repository.open(self.skills_dir)
        else:
            self.repo = Repository.init(
                self.skills_dir,
                manifest=json.dumps(manifest or DEFAULT_MANIFEST, indent=2),
            )

    # -- thin agentvcs wrappers ---------------------------------------------
    def commit(self, message: str) -> str:
        return self.repo.commit(message, author="robot")

    def rollback(self, reason: str) -> dict:
        return self.repo.rollback(reason=reason)

    def freeze(self, message: Optional[str] = None):
        # force=True: skip the eval gate (wired to a live mission in Phase 6).
        return crystallize(self.repo, message=message, force=True)

    def head(self) -> Optional[str]:
        return self.repo.head_commit()

    def history(self) -> list:
        return self.repo.log()

    def rollbacks(self) -> list[dict]:
        return self.repo.read_rollbacks()

    # -- the evolution step -------------------------------------------------
    def evolve_step(
        self,
        dream_fn: Callable[[], object],
        score_fn: Callable[[], float],
        target_skill: str = "patrol-route",
        keep_ratio: float = 0.9,
    ) -> StepResult:
        """One baseline -> dream -> commit -> re-score -> keep/rollback cycle.

        ``dream_fn()`` performs a gated skill rewrite and returns an object with a
        ``.status`` of "kept" when it changed a skill. ``score_fn()`` runs a mission
        and returns its success_rate.
        """
        baseline_score = score_fn()
        if self.head() is None:
            self.commit(f"baseline: skills at success_rate={baseline_score:.2f}")

        outcome = dream_fn()
        if getattr(outcome, "status", None) != "kept":
            return StepResult(evolved=False, baseline_score=baseline_score,
                              reason=f"dream: {getattr(outcome, 'status', 'none')}")

        commit = self.commit(f"evolve({target_skill}): {getattr(outcome, 'reason', '')}")
        new_score = score_fn()

        if new_score < baseline_score * keep_ratio:
            reason = (
                f"success_rate {new_score:.2f} < {baseline_score:.2f} baseline "
                f"(keep_ratio {keep_ratio}); revert {target_skill}"
            )
            self.rollback(reason=reason)
            return StepResult(evolved=True, kept=False, rolled_back=True,
                              baseline_score=baseline_score, new_score=new_score,
                              commit=commit, reason=reason)

        return StepResult(evolved=True, kept=True, baseline_score=baseline_score,
                          new_score=new_score, commit=commit,
                          reason="evolved skill held or improved the score")
