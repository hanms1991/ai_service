# Functional Safety Concept (FSC) Hand-off

Once the HARA produces safety goals, the next phase is the Functional Safety Concept (ISO 26262-3, Clause 7). The HARA workbook should not attempt to produce the full FSC, but it MUST produce a hand-off tab that gives the FSC author a structured starting point.

## What the FSC Hand-off tab includes

For each safety goal, the tab pre-populates:

| Column                     | Source / How it's filled                                                                          |
|----------------------------|---------------------------------------------------------------------------------------------------|
| SG_ID                      | From Safety Goals tab.                                                                            |
| Safety Goal                | "Prevent <hazard description>" — copied verbatim.                                                 |
| ASIL                       | From Safety Goals tab.                                                                            |
| Safe State                 | From Safety Goals tab.                                                                            |
| FTTI (ms)                  | Blank — analyst to fill. Reference column shows the malfunction's typical reaction-time category. |
| Allocation Target          | Blank — system / subsystem / ECU that owns the requirement.                                       |
| Warning & Degradation      | Blank — describes driver warning strategy and graceful degradation.                               |
| FSR_ID (Functional Safety Requirement) | Auto-suggested IDs `FSR-<SG_ID>-01` … `FSR-<SG_ID>-05`.                                |
| Verification method        | Picklist: Test, Analysis, Inspection, Review.                                                     |
| Decomposition (optional)   | Picklist: None, ASIL D → C(D)+A(D), ASIL D → B(D)+B(D), ASIL C → B(C)+A(C), etc.                  |

## Why this matters

The hand-off tab is the bridge between the *concept phase* (HARA + safety goals) and the *system level* work (FSC). Most HARA documents stop at safety goals and force the FSC author to recreate context. This tab carries the context forward and prevents drift between the two artifacts.

## Out of scope

The HARA xlsx does not produce the FSC document itself, the technical safety concept (TSC), the system architecture, or the safety analyses (FMEDA, FTA, DFA). Those belong to dedicated downstream skills/templates.
