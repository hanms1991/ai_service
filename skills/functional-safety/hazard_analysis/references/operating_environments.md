# Operating Environment — Cartesian Dimensions

The HARA worksheet expands each safety-critical (function × malfunction) pair across the operating environment to capture how the same hazard's S/E/C ratings shift with context.

## Default dimensions

If the user does not provide a custom environment list, the skill uses this default set. The user should always be offered the chance to trim it (some items are not used in all environments).

### Location × speed band (always paired — speed defines kinetic energy)

| Code  | Location              | Speed band (km/h) | Speed band (mph) |
|-------|-----------------------|-------------------|------------------|
| L01   | Parking lot           | 0–15              | 0–10             |
| L02   | Urban / city street   | 15–60             | 10–35            |
| L03   | Suburban arterial     | 40–80             | 25–50            |
| L04   | Rural highway         | 60–100            | 35–60            |
| L05   | Interstate / motorway | 90–130            | 55–80            |
| L06   | Off-road / unpaved    | 0–60              | 0–35             |

### Weather / surface

| Code  | Condition           |
|-------|---------------------|
| W01   | Dry, daylight       |
| W02   | Wet                 |
| W03   | Night, dry          |
| W04   | Heavy rain          |
| W05   | Snow / ice          |
| W06   | Fog                 |

### Total expansion

By default: 6 locations × 6 weather = 36 environment rows per safety-critical (function × malfunction) pair.

For a typical item with 5 functions:
- 5 × 14 = 70 (function × malfunction) candidates
- After SC filter, typically ~20–30 are SC
- 25 × 36 = 900 HARA rows

This is intentionally large. The Cartesian completeness is the value of the skill — it forces the analyst to confront edge cases they would otherwise skip.

## Pruning rules (recommended)

The skill should automatically prune env rows where:

1. The location's speed band is incompatible with the malfunction (e.g., M07 *Functions Early* on an airbag is meaningless in a parking lot — vehicle isn't in motion enough to crash).
2. The user marks an env combination as "Not credible" in the Assumptions tab.

Pruned rows are kept in the worksheet with `Status = Pruned` and a `Prune_Reason` so they remain auditable.

## Customization

The skill MUST accept a user-supplied environment list to override defaults. The most common user override is to drop *off-road* and *fog* for highway-only ADAS items, or to add fine-grained speed bands (e.g., "60–80" and "80–100" as separate rows on motorway).
