# Malfunction Guide Words (M01–M14)

Each item function MUST be evaluated against all 14 malfunction guide words. For each pair (function × malfunction), the analyst classifies the combination as:

- **Safety Critical (SC)** — A failure of this type on this function could lead to a hazardous event.
- **Not Safety Critical (NSC)** — A failure of this type on this function does not lead to a hazardous event.
- **Not Applicable (NA)** — The malfunction guide word does not physically/logically apply to this function.

When two malfunctions produce an identical system response (e.g., M02 *Stops Functioning* on a momentary actuator behaves identically to M01 *No Function*), one may be evaluated *through* the other. The skill should record this collapse explicitly in the `Subsumed_By` column rather than silently dropping the row.

## The 14 Guide Words

| ID  | Name                       | Definition                                                                                              | Typical example                                       |
|-----|----------------------------|---------------------------------------------------------------------------------------------------------|-------------------------------------------------------|
| M01 | No Function                | Function never executes when commanded.                                                                 | Brake assist commanded, no output.                    |
| M02 | Stops Functioning          | Function executes initially, then stops while still commanded.                                          | LKAS torque cuts out mid-curve.                       |
| M03 | Unrequested Function       | Function executes without being commanded.                                                              | Spontaneous brake apply at cruise.                    |
| M04 | Function Stuck             | Function holds last commanded value, ignores new commands.                                              | Throttle stuck at 30%.                                |
| M05 | Excessive Function         | Function output magnitude exceeds command.                                                              | Steering torque 2× requested.                         |
| M06 | Partial Function           | Function output magnitude is less than command, but non-zero.                                           | ABS modulates only one wheel.                         |
| M07 | Functions Early            | Function executes before the trigger condition is met.                                                  | Airbag fires before crash threshold.                  |
| M08 | Functions Late             | Function executes after the trigger condition has passed.                                               | Pre-charge applies after collision.                   |
| M09 | Function Applies Too Short | Output duration shorter than commanded.                                                                 | Brake hold releases prematurely.                      |
| M10 | Function Applies Too Long  | Output duration longer than commanded.                                                                  | EPB drag continues after release request.             |
| M11 | Function is Delayed        | Function eventually executes correctly but with latency beyond spec.                                    | Cruise resume responds 800 ms late.                   |
| M12 | Inverse Function           | Function executes in the opposite direction of the command.                                             | Steering assist torque opposite to driver input.      |
| M13 | Erratic or Intermittent    | Function output oscillates or chatters around the command.                                              | Throttle position bouncing.                           |
| M14 | Function is Uneven         | Function output has non-monotonic / asymmetric profile (e.g., one wheel braked harder than the other).  | Asymmetric brake torque causing pull.                 |

## Rating heuristics for the SC / NSC / NA filter

When auto-suggesting the SC / NSC / NA classification for a (function × malfunction) pair, use these heuristics — but the analyst must always confirm:

- **Defaults to SC**: actuators that move the vehicle (longitudinal, lateral, vertical control), display of safety-critical information, energy storage release.
- **Defaults to NSC**: HMI cosmetic features, infotainment, comfort actuators with no kinetic authority.
- **Defaults to NA**: malfunction guide words that cannot physically occur for the function (e.g., M07 *Functions Early* on a continuously-active function with no discrete trigger).

## Output requirement

When generating the HARA worksheet, every (function × M01..M14) pair MUST appear as a row in the **Function × Malfunction filter** tab, even rows classified NSC or NA, so that the analysis is auditable. Only SC pairs expand into the full Cartesian against operating environment.
