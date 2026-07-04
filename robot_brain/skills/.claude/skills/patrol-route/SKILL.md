---
name: patrol-route
description: Navigate to all facility checkpoints sequentially, observe each one, and report status.
---

## Overview

Execute a full patrol route through the facility, visiting each checkpoint in order.

## Procedure

1. **Plan the route.** Visit checkpoints in this priority order:
   - Server Room (critical infrastructure)
   - Emergency Exit (safety compliance)
   - Supply Closet (inventory security)
   - Main Entrance (perimeter check)
2. **At each checkpoint**, run the @checkpoint-inspection procedure to scan and verify it.
3. **If a person is nearby**, follow @staff-interaction to greet them and report status.
4. **Announce** each result with `speak()`: "Checkpoint [name] clear."
5. **Finish** by returning toward the Main Entrance and announcing the patrol complete.

## Navigation

- Use `observe()` to find each checkpoint by its landmark label (distance_m, bearing_deg).
- Align heading: positive bearing -> `rotate_left(deg)`, negative -> `rotate_right(deg)`.
- Move in 30-50 cm steps with `move_forward(cm)`, then `observe()` again.
- Consider a checkpoint reached within ~0.4 m; then `stop()` and move to the next.

## Anomaly detection

Flag an anomaly when:
- An unexpected person is near the Server Room or Supply Closet.
- The Fire Extinguisher is missing from its expected location.
- A checkpoint landmark is not visible from its expected distance.
