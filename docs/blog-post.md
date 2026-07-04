# I let a robot rewrite its own code. Here's what stopped it from breaking everything.

Most AI agents are frozen at birth.

They ship with a fixed set of skills, and when they fail, a human reads the logs, edits a prompt, and redeploys. The agent never learns. *We* learn, slowly, on its behalf.

I wanted to test a different idea:

> **An agent that gets better at its job on its own — safely, reversibly, with a full audit trail — running on a commodity model with no GPU.**

So I built [**evolving-robot**](https://github.com/EvolvingAgentsLabs/evolving-robot): a small 2D patrol robot that runs a mission, notices where it did poorly, **rewrites one of its own skills**, and only keeps the change if it provably didn't break anything *and* improved its score. Otherwise the change is rolled back — with the reason on record.

It works. And the interesting part isn't the robot. It's the three guardrails that made "let it edit itself" a sane thing to do.

## The scary question

The moment an agent can edit its own skills, you inherit a problem: **what stops it from writing something broken?**

A hallucinated reference to a skill that doesn't exist. A name collision. A malformed file. Any of these can quietly poison the agent's behavior — and self-editing without a safety net isn't evolution, it's entropy.

My answer was to make the loop pass through three independent checkpoints, each owned by an open-source project that solves exactly one hard part:

**1. A scoreboard.** [odyssey](https://github.com/lovellai-dev/odyssey) (by [@SoyGema](https://github.com/SoyGema)) runs and scores every patrol mission. An agent can only improve if it's *measured* — odyssey gives me an honest `success_rate` at the end of every run, and its protocol seams are so clean that a REST-based Gemma brain and a toy 2D simulator slotted in without forking anything.

**2. A linter for skills.** [skill-map](https://github.com/crystian/skill-map) (by [@crystian](https://github.com/crystian)) reads every skill file, builds a graph of how they reference each other, and turns broken references, collisions, and schema violations into hard errors. Every self-edit must pass this gate before it can land. If the rewrite is broken, the exact error goes back to the model to try again — and if it still can't produce something clean, the edit is discarded.

**3. A genetic memory.** [agentvcs](https://github.com/EvolvingAgentsLabs/agentvcs) is version control built for agents: a commit pins code + goal + the trace of what the agent actually did, as one object. It gives the loop its two survival instincts — `rollback(reason=…)` when a change makes things worse, and `crystallize` to freeze a verified skill set into a trusted recipe.

## The loop

One full cycle looks like this:

1. **Run** a patrol mission → get a score.
2. **Commit** the current skills, with the mission trace attached.
3. **Dream**: the model reads its own failure trace and rewrites the weakest skill.
4. **Gate**: skill-map rejects anything broken; the model retries with the error as feedback.
5. **Re-score**: improved → keep and freeze. Regressed → rollback, reason recorded.

Run mission → score → commit → dream → gate → re-score → keep or rollback → freeze.

## What actually happened

None of this is hypothetical — every claim above is something I ran and verified:

- The dream engine rewrote its `patrol-route` skill from its own failure trace, producing a tighter navigation loop. **No human touched the skill.**
- One rewrite hallucinated a reference to a `@ghost-skill` that didn't exist. skill-map caught it, the model got the error back, and the bad edit **never survived**.
- A regression triggered a rollback, and the ledger says exactly why: `success_rate 0.40 < 1.00 baseline; revert patrol-route`.
- The whole brain — planner, pilot, and skill-writer — is **one commodity model**: Gemma 4 over Google AI Studio's REST API. No GPU. No local weights. No real hardware.

## The takeaway

You can let an agent improve itself — if a **gate** keeps it honest, a **scoreboard** keeps it accountable, and **version control** keeps every step reversible.

None of those three pieces is exotic. They're open source, they compose beautifully, and the entire proof runs on a laptop with an API key.

The code, the seven-phase build log, and the full write-up are here:
👉 [github.com/EvolvingAgentsLabs/evolving-robot](https://github.com/EvolvingAgentsLabs/evolving-robot)

*Built on [skill-map](https://skill-map.ai/), [odyssey](https://github.com/lovellai-dev/odyssey), and [agentvcs](https://github.com/EvolvingAgentsLabs/agentvcs). If you're building agents that need to change themselves, start with the guardrails, not the loop.*
