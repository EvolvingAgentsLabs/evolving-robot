---
name: staff-interaction
description: Greet facility staff, report patrol status, and handle simple spoken requests.
---

## Overview

Interact with facility staff during a patrol: greet them, report findings, and respond to
requests. Called from @patrol-route and @checkpoint-inspection when a person is detected.

## Procedure

1. **Detect** the nearest person via `observe()` (nearest_person, with distance and bearing).
2. **Face them**: align heading using `rotate_left` / `rotate_right` toward their bearing.
3. **Greet** with `speak()`:
   - "Hello, I am the facility patrol robot. Current status: [status]."
4. **Handle common requests** (announced back with `speak()`):
   - "Check [location]" -> go there and run @checkpoint-inspection.
   - "Report" / "Status" -> speak the current patrol summary.
   - "Continue" / "All clear" -> resume @patrol-route.
   - "End patrol" -> return toward the Main Entrance and `stop()`.
5. **Confirm** any command before acting: "Understood, checking the [location] now."

## Communication style

- Professional and concise - this is a work environment.
- State facts, not opinions.
- Report anomalies immediately: "Alert: [description]."
