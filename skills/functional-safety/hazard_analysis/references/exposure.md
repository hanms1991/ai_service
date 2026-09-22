# Exposure (E) — Probability of the Operational Situation

**Critical clarification (from the user's HARA practice):** Exposure is rated against the **operational situation / environment**, NOT against the hazard itself. The question is: *how often is the vehicle in the situation where this hazard could occur?* — not *how often does the hazard occur?*

Hazard frequency is captured implicitly by the combination of E (situation frequency) and the underlying random hardware failure rate addressed downstream (PMHF / FIT analysis). Conflating the two double-counts probability and inflates ASIL inappropriately.

## ISO 26262 Exposure Bands

| E   | Probability of the operational situation                  | Quantitative anchor                              | Examples                                                                              |
|-----|-----------------------------------------------------------|--------------------------------------------------|---------------------------------------------------------------------------------------|
| E0  | Incredibly unlikely                                       | < 10⁻⁴ of average operating time                 | Vehicle on a closed test track being chased by a moose. (Effectively never.)          |
| E1  | Very low probability                                      | < 1% of average operating time, < once per year  | Towing a heavy trailer down an alpine pass in winter.                                 |
| E2  | Low probability                                           | 1–10% of operating time, < once per month        | Driving in heavy snowfall. Towing in rain.                                            |
| E3  | Medium probability                                        | 10–50% of operating time, < once per week        | City driving with stop-and-go traffic. Wet road. Night driving.                       |
| E4  | High probability                                          | > 50% of operating time, > once per week         | Highway cruising. Dry pavement. Daytime.                                              |

## Operational situation taxonomy (used for the Cartesian expansion)

The skill expands each safety-critical (function × malfunction) pair across these dimensions:

### Location / road type (with speed bands)
| Location          | Typical speed band (km/h) | Typical speed band (mph) | Default E |
|-------------------|---------------------------|--------------------------|-----------|
| Parking lot       | 0–15                      | 0–10                     | E3        |
| Urban / city      | 15–60                     | 10–35                    | E4        |
| Suburban arterial | 40–80                     | 25–50                    | E4        |
| Rural highway     | 60–100                    | 35–60                    | E3        |
| Interstate / motorway | 90–130                | 55–80                    | E4        |
| Off-road / unpaved | 0–60                     | 0–35                     | E1        |

### Weather / road surface
| Condition         | Default E |
|-------------------|-----------|
| Dry pavement, daylight   | E4 |
| Wet pavement      | E3 |
| Night, dry        | E3 |
| Heavy rain        | E2 |
| Snow / ice        | E2 |
| Fog               | E1 |

### Traffic density (modifier, not a separate row)
| Density           | Notes |
|-------------------|-------|
| Free flow         | Use base E |
| Moderate          | Use base E |
| Congested         | May raise S by 1 band (rear-end), no change to E |

## Combining factors

When two situational factors are combined (e.g., *highway* + *snow*), the combined E is the **lower** (rarer) of the two band values. Document the reasoning in `E_Rationale`. Example:

> E2. Interstate driving (E4) combined with snow / ice (E2). Co-occurrence governed by the rarer condition. Snowfall affects ~5% of annual operating time for the average North American driver.

## Auto-suggest output

`E_Rationale` MUST cite the location band, the weather condition, and the combined band selected. The skill should never assign an E without a documented rationale because exposure assumptions are the most-challenged element of any HARA review.
