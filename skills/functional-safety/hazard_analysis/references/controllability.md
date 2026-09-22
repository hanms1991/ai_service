# Controllability (C) — Driver's Ability to Avoid Harm

Controllability rates the **average driver's** ability to avoid the harm once the hazardous event occurs — not an expert's, not the worst-case driver's.

| C   | Description           | Quantitative anchor                                               | Examples                                                                  |
|-----|-----------------------|-------------------------------------------------------------------|---------------------------------------------------------------------------|
| C0  | Controllable in general | Hazard avoided by any reasonable driver action with high reliability | Slowly drifting cruise control speed creep on an empty highway.           |
| C1  | Simply controllable   | ≥99% of drivers (or normal drivers) can avoid the harm.            | Loss of brake assist (still have hydraulic pedal, just heavier).          |
| C2  | Normally controllable | ≥90% of drivers can avoid the harm with normal reaction.           | Sudden deceleration request of moderate magnitude during cruise.          |
| C3  | Difficult to control / uncontrollable | <90% of drivers can avoid the harm, or no useful driver action exists. | Unintended full braking on one wheel at highway speed (yaw moment too fast for the average driver to counter). |

## Practical guidance for assigning C

1. **Time-to-react matters.** If the average driver has < 0.5 s to perceive and act, C is almost always C3.
2. **Driver authority over the hazard.** If turning the steering wheel, releasing the throttle, or braking can resolve the hazard within available time → C1 or C2. If the hazard *fights* the driver (e.g., M12 *Inverse Function*) → C3.
3. **Sensory cues.** A hazard with strong proprioceptive feedback (e.g., a yank on the steering wheel) is more controllable than a silent sensor-output drift.
4. **Speed regime.** Higher speed → less reaction time → higher C. The skill biases C upward by one band when the speed band is > 100 km/h, unless rationale overrides.
5. **Cognitive load.** A driver doing a primary driving task (steering through a curve in rain) has less attention available; bias C upward.

## Common mistake to avoid

Do not assume the driver has prior knowledge of the failure mode. The C rating represents an **untrained** average driver experiencing the event for the first time.

## Auto-suggest output

`C_Rationale` MUST cite (a) the available reaction time estimate, (b) whether the driver has authority over the hazard direction, and (c) the speed regime. Example:

> C3. Inverse steering torque (M12) on interstate. Driver expects assist in commanded direction; receives torque opposing input. Time to recognize and counter < 0.5 s at 110 km/h. <90% of average drivers can avoid lane departure.
