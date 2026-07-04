---
name: checkpoint-inspection
description: Perform a detailed inspection at a single checkpoint - scan, verify, and report findings.
---

## Overview

Conduct a thorough inspection at one checkpoint location. Used when a specific area needs
detailed checking, typically called from @patrol-route.

## Procedure

1. **Arrive** at the checkpoint using `observe()`, `rotate_left`/`rotate_right`, and `move_forward`.
2. **Perform a 360-degree scan**:
   a. `observe()` at the current heading.
   b. `rotate_right(90)` then `observe()` - repeat three more times to cover the full circle.
3. **Note findings** from each observation:
   - People present (bearing and distance).
   - Door / access-point landmarks in range.
   - Objects present or missing (e.g. Fire Extinguisher).
   - Any landmark closer or farther than expected.
4. **If a person is present**, hand off to @staff-interaction before continuing.
5. **Report** with `speak()`: "Checkpoint [name] - status [clear / anomaly]."

## Safety priorities

1. Fire-safety equipment present and accessible.
2. Emergency exits unobstructed.
3. Restricted areas (Server Room) secured.
4. No unexpected personnel in sensitive areas.
