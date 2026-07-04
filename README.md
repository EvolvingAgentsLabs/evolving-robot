# evolving-robot

A 2D patrol robot that **evolves its own skills autonomously**. One brain (**Gemma 4**
over Google AI Studio, no GPU), orchestrated by [**odyssey**](https://github.com/lovellai-dev/odyssey), gated by
[**skill-map**](https://github.com/crystian/skill-map), and versioned by [**agentvcs**](https://github.com/EvolvingAgentsLabs/agentvcs). The robot
patrols a simulated facility, dreams about its failures, rewrites a skill, and only keeps
the change if it survives a semantic gate and improves its score, otherwise agentvcs rolls
it back.

📖 **New here?** Read [**How evolving-robot works — and the shoulders it stands on**](./docs/how-it-works.md)
for the story: the promise, the three open projects behind it, and how the loop fulfills it.
See [`PLAN.md`](./PLAN.md) for the full architecture, and the
[project page](https://evolvingagentslabs.github.io/evolving-robot/) for a visual overview.

## Status

- [x] **Phase 0** — scaffolding + Gemma brain over REST (`robot_brain/gemma.py`)
- [x] **Phase 1** — 2D simulator + WebSocket transport + live browser viewer (`sim2d/`)
- [x] **Phase 2** — odyssey end to end (Gemma planner + 2D pilot + `Sim2DRunner`)
- [x] **Phase 3** — skills in Claude Code layout + skill-map gate (`robot_brain/skill_gate.py`)
- [x] **Phase 4** — dream engine: self-rewriting skills, gated + reverted (`robot_brain/dream.py`)
- [x] **Phase 5** — agentvcs commit / rollback / freeze in the evolution loop (`robot_brain/evolve.py`)
- [x] **Phase 6** — agentvcs dogfooding (`rollback(reason=)` + `odyssey` trace provider) + live loop

## Quick start

```bash
python3.12 -m venv .venv && ./.venv/bin/pip install httpx websockets
cp .env.example .env    # add GEMINI_API_KEY (AI Studio) or OPENROUTER_API_KEY
```

**Phase 0 - brain smoke test** (needs an API key; skips cleanly without one):

```bash
set -a; source .env; set +a
./.venv/bin/python scripts/smoke_gemma.py
```

**Phase 1 - drive the simulator:**

```bash
./.venv/bin/python -m sim2d.server      # terminal 1
open http://localhost:9092              # browser: live arena viewer
./.venv/bin/python scripts/drive_sim.py # terminal 2: scripted 4-point patrol
```

You should see the robot rotate + advance to each checkpoint and `observe()` report the
landmarks in range, both in the terminal and animated in the viewer.

**Phase 2 - run the patrol as an odyssey mission** (Gemma plans + pilots; falls back to a
scripted geometry pilot with no API key):

```bash
./.venv/bin/python -m sim2d.server         # terminal 1 (+ open the viewer)
set -a; source .env; set +a                # optional: enables the Gemma pilot
./.venv/bin/python scripts/run_mission.py  # terminal 2
```

odyssey runs the mission end to end (warm-up training stub -> `cpu_mock`, patrol eval ->
`Sim2DRunner`) and prints `overall_grade` + the eval `result_summary`
(`success_rate` / `passed`).

**Phase 3 - the skill-map gate** (needs `sm` on Node >= 24; the gate auto-resolves an
nvm-installed v24+, or set `SM_CMD`):

```bash
node --version                              # must be >= 24 for sm; `nvm install 24` if not
./.venv/bin/python scripts/gate_demo.py     # accept clean set, reject a broken @ref, accept after revert
```

Skills live in the Claude Code layout under `robot_brain/skills/.claude/skills/<name>/SKILL.md`,
so `sm scan` detects them and `gate_skill()` rejects any edit that introduces a broken
cross-reference, name collision, or schema violation.

**Phase 4 - the dream engine** (self-rewriting skills; a mission writes a trace to `traces/`
first, and the keep path needs a Gemma key):

```bash
./.venv/bin/python scripts/dream_demo.py
```

The engine reads the latest trace, asks Gemma to rewrite `patrol-route`, and gates the
rewrite: a valid one is kept, a broken one is retried with skill-map's feedback and reverted.
The Gemma pilot reads the active skill body, so an evolved skill actually changes behavior.

**Phase 5 - the evolution loop** (agentvcs versions the skills; deterministic, offline):

```bash
./.venv/bin/python scripts/evolve_demo.py
```

`EvolutionController` commits the skills, and on each evolution: a regression triggers
`rollback(reason=...)` (restoring the skill files, recording why), while a good rewrite is
kept and then frozen (`crystallize`). Runs on a throwaway copy with injected scores; the live
loop wires the real dream + a real mission score (see the note in the script).

**Phase 6 - the live loop** (starts the sim, runs a real mission, commits with the odyssey
trace, dreams, re-scores; restores the skills afterward unless `--keep`):

```bash
./.venv/bin/python scripts/evolve_live.py
```

### agentvcs dogfooding

Building this surfaced two improvements, implemented and tested in the sibling
[`agentvcs`](https://github.com/EvolvingAgentsLabs/agentvcs) repo (190 tests pass):

1. **`Repository.rollback(reason=...)`** - records *why* a rollback happened (an eval
   regression) in the durable ledger, instead of only the restored goal text.
2. **The `odyssey` trace provider** (`agentvcs/src/agentvcs/traces/odyssey.py`) - reads
   odyssey's native `missions.db` and normalizes a mission run (objective + per-task
   `result_summary` + grade) into agentvcs's message list, so commits version *what the
   mission actually produced*. Wired via `"trace": {"provider": "odyssey", ...}` in the
   controller's manifest.

## Layout

```
robot_brain/gemma.py     Gemma over REST (AI Studio + OpenRouter). generate(messages,image)->str
                         matches odyssey's TextGenerator; generate_full() exposes tool-calls.
robot_brain/pilot.py     Pilot2D.act(obs,instruction)->action. gemma (function-calling) | scripted
sim2d/server.py          SimulatorHAL (trig, no physics) + WS(:9091) + HTTP(:9092)
sim2d/viewer.html        Live canvas viewer (arena, robot, trail, observe radius, event log)
odyssey_ext/gemma_rest.py  Adapts the brain to odyssey's TextGenerator (LLMPlanner)
odyssey_ext/sim2d_runner.py  odyssey eval Runner: PlannedEvalRuntime(Gemma planner+pilot) over WS
missions/patrol.mission.yaml  training stub (cpu_mock) + custom eval (Sim2DRunner)
robot_brain/skills/.claude/skills/*/SKILL.md  the 3 skills (skill-map layout, @cross-refs)
robot_brain/skills.py    parse skills + progressive disclosure + write_skill_body
robot_brain/skill_gate.py  gate_skill(): sm scan --changed + sm check --json -> accept/reject
robot_brain/dream.py     DreamEngine: trace -> Gemma rewrites a skill -> gate -> keep/revert
robot_brain/evolve.py    EvolutionController: agentvcs init/commit/rollback(reason)/freeze by score
scripts/smoke_gemma.py   Phase 0 acceptance
scripts/drive_sim.py     Phase 1 acceptance (scripted geometry pilot)
scripts/run_mission.py   Phase 2 acceptance (odyssey engine, providers=None; writes a trace)
scripts/gate_demo.py     Phase 3 acceptance (skill-map gate)
scripts/dream_demo.py    Phase 4 acceptance (self-rewrite: discard + keep paths)
scripts/evolve_demo.py   Phase 5 acceptance (agentvcs commit/rollback/freeze by score)
scripts/evolve_live.py   Phase 6 live loop (sim + real mission + dream + agentvcs, odyssey trace)
```

The brain, simulator, and viewer event vocabulary are ported from
[`skillos_x_robot`](https://github.com/EvolvingAgentsLabs/skillos_x_robot); everything else is new to this project.

## Providers

Set `ROBOT_PROVIDER=aistudio` (default) or `openrouter`. Both are GPU-free.
`GEMMA_MODEL` defaults to `gemma-4-26b-a4b-it`, confirmed working on AI Studio via
`:generateContent` (text + native function-calling). Note: gemma-4 does "thinking" by
default, which leaks into the text output and bills extra tokens.

## Acknowledgments

evolving-robot stands on three open projects. This example exists to show how well they
compose — full credit and thanks to their authors. The full story is in
[**How evolving-robot works**](./docs/how-it-works.md).

<p>
  <a href="https://skill-map.ai/"><img src="./docs/img/skill-map.png" alt="skill-map" width="49%"></a>
  <a href="https://odyssey.dev/"><img src="./docs/img/odyssey.png" alt="odyssey" width="49%"></a>
</p>

- **[skill-map](https://github.com/crystian/skill-map)** — [skill-map.ai](https://skill-map.ai/) —
  by **[@crystian](https://github.com/crystian)**. The semantic gate that makes the robot's
  self-edits safe: it models every skill/agent/command as a graph and rejects broken
  references, name collisions, and schema violations before an edit can land.
- **[odyssey](https://github.com/lovellai-dev/odyssey)** — by **[@SoyGema](https://github.com/SoyGema)**
  (lovell AI). The mission-orchestration and evaluation framework that runs and scores the
  robot's patrols. Its clean `TextGenerator` / `Runner` / `PlannedEvalRuntime` seams let us
  drop in a REST-based Gemma brain and a 2D-sim runner without touching its core.
- **[agentvcs](https://github.com/EvolvingAgentsLabs/agentvcs)** — the multidimensional
  version control that gives the robot a genetic memory: commit code + goal + mission trace
  together, roll back a regression with a recorded reason, and freeze a verified skill set.

Building this loop contributed two improvements back to agentvcs (see
[agentvcs dogfooding](#agentvcs-dogfooding) above): `Repository.rollback(reason=…)` and the
`odyssey` trace provider.

The robot's brain, 2D HAL, and dream pattern were adapted from
[`skillos_x_robot`](https://github.com/EvolvingAgentsLabs/skillos_x_robot).

## License

[Apache 2.0](./LICENSE) © Evolving Agents Labs.
