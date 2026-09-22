# Severity (S) — Mapped to the Abbreviated Injury Scale (AIS)

ISO 26262-3:2018, Annex B uses the AIS (Association for the Advancement of Automotive Medicine) as the canonical injury reference. Severity is rated against the **occupants of the subject vehicle and other road users involved in the hazardous event**.

| ISO S | AIS bands                                  | Description                                                  | Concrete examples                                                                              |
|-------|--------------------------------------------|--------------------------------------------------------------|------------------------------------------------------------------------------------------------|
| S0    | AIS 0 (no injury) and material-only damage | No injuries.                                                  | Bumper scrape in a parking maneuver. Cosmetic damage only.                                     |
| S1    | AIS 1 to AIS 2 (≥10% probability)          | Light and moderate injuries.                                  | Bruises, sprains, minor lacerations. Side-swipe at low speed.                                  |
| S2    | AIS 3 to AIS 6 (<10% probability) plus AIS 1–2 (>10%) | Severe and life-threatening injuries (survival probable). | Concussion, multiple fractures, internal injury where survival is likely.                      |
| S3    | AIS 3 to AIS 6 (≥10% probability)          | Life-threatening (survival uncertain) to fatal injuries.      | High-speed unintended steering, full unintended braking on highway, loss of vehicle control at speed. |

## Practical guidance for assigning S

1. **Speed bands matter.** The same hazardous event at 25 km/h vs 130 km/h often differs by one or two S levels. The skill creates one HARA row per speed band per location, so the S rating is bound to a specific kinetic energy regime.
2. **Don't double-count exposure.** The frequency or duration of the operational situation belongs in **E**, not S. S asks: *given the event happens, how bad is it?*
3. **Vulnerable road users.** If the hazardous event commonly involves pedestrians, cyclists, or motorcyclists, bias S upward by one band.
4. **Crash energy heuristic** (purely as a sanity check, not a rule):
   - < 20 km/h closing speed → typically S0–S1
   - 20–40 km/h → typically S1–S2
   - 40–80 km/h → typically S2–S3
   - > 80 km/h → typically S3
5. **Multi-vehicle pile-up scenarios** (e.g., highway loss of stability) bias upward by one band due to secondary impacts.

## Auto-suggest output

When the skill drafts an S rating, the row's `S_Rationale` cell MUST cite (a) the speed band, (b) the AIS band assumed, and (c) the reasoning. Example:

> S3. Highway speed band 100–130 km/h. Sudden full unintended brake apply on a single rear wheel induces yaw at high speed; loss of control with high probability of secondary impact. AIS 3–6 ≥10% probability.
