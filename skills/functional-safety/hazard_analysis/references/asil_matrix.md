# ASIL Determination Matrix (ISO 26262-3:2018, Table 4)

ASIL is determined from the triple (S, E, C). Any combination involving S0 yields no safety requirement (denoted "—"). Any combination involving E0 yields no ASIL classification (the situation is too rare to drive a safety requirement on its own).

## Lookup Table

|         | C1   | C2   | C3   |
|---------|------|------|------|
| S1, E1  | QM   | QM   | QM   |
| S1, E2  | QM   | QM   | QM   |
| S1, E3  | QM   | QM   | A    |
| S1, E4  | QM   | A    | B    |
| S2, E1  | QM   | QM   | QM   |
| S2, E2  | QM   | QM   | A    |
| S2, E3  | QM   | A    | B    |
| S2, E4  | A    | B    | C    |
| S3, E1  | QM   | QM   | A    |
| S3, E2  | QM   | A    | B    |
| S3, E3  | A    | B    | C    |
| S3, E4  | B    | C    | D    |

S0 → "—" (no safety requirement). E0 → "QM" regardless of S, C.

## Spreadsheet implementation

The HARA xlsx implements this lookup as an INDEX/MATCH against a hidden lookup table on the `10_ASIL_Matrix` tab. Do NOT hardcode ASIL values into the HARA worksheet — drive every ASIL cell from the formula so that if an analyst changes an S, E, or C input, ASIL recomputes immediately. Use this formula pattern in the HARA tab:

```
=IFERROR(INDEX('10_ASIL_Matrix'!$D$4:$F$15,
              MATCH(S_cell&"-"&E_cell, '10_ASIL_Matrix'!$A$4:$A$15&"-"&'10_ASIL_Matrix'!$B$4:$B$15, 0),
              MATCH(C_cell, '10_ASIL_Matrix'!$D$3:$F$3, 0)),
       IF(S_cell="S0","—",IF(E_cell="E0","QM","CHECK")))
```

Because that's an array formula, the actual generated formula uses a precomputed key column to keep it as a regular formula:

```
=INDEX(LookupArray, MATCH(KeyCell, KeyColumn, 0), MATCH(C_cell, ClassHeader, 0))
```

where `KeyCell = S_cell & "-" & E_cell` is built in a helper column.

## ASIL Decomposition (mentioned, not implemented)

ISO 26262-9 permits decomposing an ASIL across redundant subsystems (e.g., ASIL D → ASIL B(D) + ASIL B(D) on independent channels). The HARA records the original (top-level) ASIL only. Decomposition belongs to the FSC / TSC and is out of scope for the HARA worksheet itself, though the FSC Hand-off tab includes a column where decomposition decisions can be recorded as the analysis matures.
