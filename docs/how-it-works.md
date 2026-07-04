# How evolving-robot works — and the shoulders it stands on

## The promise

Most agents are frozen at birth. They ship with a fixed set of skills, and when they fail,
a human edits the prompt or the code and redeploys. **evolving-robot** is a small, honest
proof of a different idea:

> An agent that gets better at its job **on its own** — safely, reversibly, and with a full
> audit trail — running on a commodity model with no GPU.

Concretely: a 2D patrol robot runs a mission, notices where it did poorly, **rewrites one of
its own skills** to fix it, proves the rewrite didn't break anything, and keeps the change
only if it actually improves the score. If it doesn't, the change is rolled back and the
reason is recorded. Every mutation is versioned like source code.

That promise only holds because three existing open projects each solve one hard part of it.
This is the story of how they fit together — and a thank-you to the people who built them.

---

## 1. skill-map — the safety gate

[![skill-map](img/skill-map.png)](https://skill-map.ai/)

> [**skill-map.ai**](https://skill-map.ai/) · [github.com/crystian/skill-map](https://github.com/crystian/skill-map)

The moment an agent can edit its own skills, you inherit a scary question: *what stops it
from writing something broken?* A hallucinated reference to a skill that doesn't exist, a
name that collides with another, a malformed file — any of these can quietly poison the
agent's behavior.

[**skill-map**](https://github.com/crystian/skill-map) is the answer. It reads every skill,
agent, command, and doc in an AI harness and builds a single **graph** of how they reference
each other. Then it validates that graph: broken references, name collisions, and schema
violations become hard errors you can gate on. It's the "linter" for a world where the
program is a folder of Markdown files.

In evolving-robot, the robot's skills live as `SKILL.md` files that reference each other
(`@checkpoint-inspection`, `@staff-interaction`). Before **any** self-edit is allowed to
land, we run `sm scan` + `sm check`. If the robot's rewrite introduces a dangling reference,
skill-map catches it, we hand the exact error back to the model to try again, and if it still
can't produce something clean, the edit is discarded. skill-map is what makes autonomous
self-editing *safe* instead of reckless.

Huge thanks to **[@crystian](https://github.com/crystian)** for building it — and for the
clean `sm check --json` contract that made it trivial to wire into a gate.

---

## 2. odyssey — the body, the arena, and the score

[![odyssey](img/odyssey.png)](https://odyssey.dev/)

> [**odyssey.dev**](https://odyssey.dev/) · [github.com/lovellai-dev/odyssey](https://github.com/lovellai-dev/odyssey)

An agent can only improve if it is *measured*. You need a world to act in, a task to attempt,
and an honest number at the end.

[**odyssey**](https://github.com/lovellai-dev/odyssey) is a framework for defining, running,
and benchmarking robot missions. It orchestrates the whole run — training and evaluation
tasks, a loadout of agents (a planner, a pilot), episodes, and scoring — and it does it
through beautifully small seams. Its `TextGenerator`, `Runner`, and `PlannedEvalRuntime`
abstractions are duck-typed protocols, so you can swap in *your* model and *your* simulator
without forking anything.

That design is what let evolving-robot exist at all. We didn't need a GPU cluster or a real
robot: we wrote a `TextGenerator` that calls **Gemma over REST**, a `Runner` that drives a
tiny 2D simulator over WebSocket, and dropped them straight into odyssey's multi-agent
runtime. odyssey plans the patrol, pilots the robot, scores each episode, and hands us back a
`success_rate` — the exact signal the evolution loop turns on.

Thank you to **[@SoyGema](https://github.com/SoyGema)** and Lovell AI for odyssey, and for
protocol seams so clean that a REST-based Gemma brain and a 2D toy world slotted in without a
single change to the core.

---

## 3. agentvcs — the genetic memory

> [github.com/EvolvingAgentsLabs/agentvcs](https://github.com/EvolvingAgentsLabs/agentvcs)

Self-improvement without memory is just drift. If an agent changes itself and you can't see
*what* changed, *why*, or *how to undo it*, you don't have evolution — you have entropy.

[**agentvcs**](https://github.com/EvolvingAgentsLabs/agentvcs) is version control built for
agents. A commit doesn't just pin code; it pins **code + goal + models + the trace of what
the agent actually did**, all as one object. And it gives an agent the two operations
evolution needs:

- **`rollback`** — the panic button. When a change makes things worse, restore the full prior
  state and record *why* in a durable ledger.
- **`crystallize` (freeze)** — when a change is verified good, freeze it into a trusted,
  replayable recipe.

This is exactly the shape a learning loop wants: try a mutation, keep it if it helps, revert
it if it hurts, and freeze what works — with every step on the record. agentvcs turns "the
agent edited itself" from something you fear into something you can audit and trust.

---

## 4. How it all comes together

evolving-robot is the closed loop those three tools make possible. One run:

1. **odyssey** runs a patrol mission in the 2D sim; **Gemma** plans the route and pilots the
   robot. odyssey returns a `success_rate`.
2. **agentvcs** commits the current skills — capturing odyssey's mission trace alongside the
   code and goal.
3. The **dream engine** reads the trace, and **Gemma** rewrites the skill that's holding the
   robot back.
4. **skill-map** gates the rewrite. Broken reference? Feed the error back and retry, or
   discard.
5. odyssey runs the mission again. If the score dropped, **agentvcs rolls back** — and the
   ledger records `success_rate 0.40 < 1.00 baseline; revert patrol-route`. If it held or
   improved, the skill is kept, and a verified set is **crystallized**.

And the whole brain is a single model — **Gemma 4**, called over Google AI Studio's REST API.
No GPU. No local weights. No real hardware. The planner, the pilot, and the skill-writer are
all the same commodity model.

```
run mission (odyssey + Gemma) → score
   → commit (agentvcs, with the odyssey trace)
   → dream: rewrite a skill (Gemma)
   → gate (skill-map): reject broken edits, retry with feedback
   → re-score → keep, or rollback(reason) → freeze when verified
```

---

## The promise, fulfilled

Every piece of that promise is not a claim — it's something we ran and verified end to end:

- **Gets better on its own.** The dream engine rewrote `patrol-route` from its own failure
  trace, producing a tighter navigation loop — no human touched the skill.
- **Safely.** A rewrite that hallucinated a broken `@ghost-skill` reference was caught by
  skill-map, retried with its feedback, and then cleanly reverted. The bad edit never
  survived.
- **Reversibly, with an audit trail.** A regression triggered `agentvcs rollback(reason=…)`,
  restoring the skill files and recording exactly why; a good set ended up `crystallized`.
- **On a commodity model, no GPU.** Confirmed on `gemma-4-26b-a4b-it` over AI Studio —
  native function-calling drives the robot, and the same model rewrites the skills.

That's the whole idea: an agent you can let improve itself, because a gate keeps it honest,
a scoreboard keeps it accountable, and version control keeps every step reversible.

Building this loop even gave two improvements back to agentvcs —
[`Repository.rollback(reason=…)`](https://github.com/EvolvingAgentsLabs/agentvcs) and an
[`odyssey` trace provider](https://github.com/EvolvingAgentsLabs/agentvcs/tree/main/examples/odyssey) —
which is exactly what good dogfooding is supposed to do.

---

*Built with [skill-map](https://skill-map.ai/) by [@crystian](https://github.com/crystian),
[odyssey](https://github.com/lovellai-dev/odyssey) by [@SoyGema](https://github.com/SoyGema),
[agentvcs](https://github.com/EvolvingAgentsLabs/agentvcs), and
[Gemma 4](https://aistudio.google.com/). Thank you.*
