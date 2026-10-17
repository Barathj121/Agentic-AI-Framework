# ClariFi combined investigation sample

## Upload

Choose **ClariFi Agent → Combined investigation → Upload Excel workbook**. Upload `clarifi-combined-review.xlsx`, choose a model, review the source records, approve and run. Use Offline rules demo to reproduce the baseline without an AI call. Gemini/Local Gemma adds planning and narrative; the same reviewed tools compute the scores.

## Data provenance

All operational rows are SIMULATED. Historical names identify public-case references only. Generated prices, amounts, dates, account/employee IDs, issuer IDs, access assertions, relationship declarations, addresses, bank accounts, ownership edges and payment cycles are not historical evidence. The rows are not a reconstruction of complete case ledgers.

- Insider reference: [SEC, 30 May 2023](https://www.sec.gov/newsroom/press-releases/2023-98). Ishan Wahi's confidential-listing access and tipping to Nikhil Wahi and Sameer Ramani motivated the related-account scenario. `SIM_R_WAHI` is an invented onboarding declaration, not an assertion that Coinbase had one. `SIM_LISTING` and all trade dates/prices are invented.
- Company reference: [DOJ civil forfeiture complaint, filed 1 July 2020, printed pages 17–18, paragraphs 44–48](https://www.justice.gov/opa/press-release/file/1291146/dl). The complaint alleges diversion to Good Star Limited and describes its control. `SIM_RISK_0_0` uses its name as a case reference; every bank/address/owner/payment connection in this sample is invented. The three-company cycle is a detector exercise, not an alleged 1MDB transaction path.

## Volume

| Table | Rows |
|---|---:|
| employees | 120 |
| accounts | 160 |
| relationships | 11 |
| trades | 900 |
| events | 12 |
| entities | 90 |
| payments | 1200 |
| **Total evidence rows** | **2493** |

## Baseline results

Computed by the actual reviewed tools with the offline reasoner. Scores prioritize review and are not fraud probabilities. No real-world accuracy is established by these constructed examples. This is a demonstration fixture; do not use it as a held-out accuracy benchmark.

| Capability | Disposition | Findings |
|---|---|---:|
| insider_trading | analyst_review | 30 |
| insider_trading | identity_review | 10 |
| insider_trading | insufficient_evidence | 860 |
| shell_companies | analyst_review | 9 |
| shell_companies | routine_review | 81 |

## Cases to inspect

| Finding | Score | Disposition |
|---|---:|---|
| insider:SIM_T_DIRECT_00:SIM_E001 | 100 | analyst_review |
| insider:SIM_T_FAMILY_00:SIM_E001 | 100 | analyst_review |
| insider:SIM_T_WAHI_00:SIM_E_WAHI | 95 | analyst_review |
| shell:SIM_RISK_0_0 | 90 | analyst_review |
| insider:SIM_T_NO_PRECLEARANCE_DATA:SIM_E011 | 85 | analyst_review |
| insider:SIM_T_NO_ISSUER_ACCESS:SIM_E012 | 75 | insufficient_evidence |
| insider:SIM_T_AT_PUBLIC_EVENT:SIM_E013 | 70 | insufficient_evidence |
| insider:SIM_T_INITIALS_00:SIM_E001 | 60 | identity_review |
| shell:SIM_ADDRESS_ONLY_0 | 15 | routine_review |
| shell:SIM_TREASURY_0 | 15 | routine_review |
| shell:SIM_MIXED_0 | 5 | routine_review |
| insider:SIM_T_UNLINKED_FRIEND:unresolved | 0 | insufficient_evidence |

The unlinked friend is intentionally unresolved: without a supplied identity/relationship link the tool cannot recover a tipping network from news. Initials remain identity-review leads. Missing preclearance data adds no points; an exact event-time trade is outside the strictly-before window. Mixed-currency edges do not form one cycle. Shared addresses alone do not establish wrongdoing. Documented treasury cycles receive contextual score reductions.

The baseline report contains all findings. AI narrative is limited to the highest 25 findings, so it is not a narrative for every row. On the initial overview, the payment chart shows at most 100 rows per currency; use finding evidence to inspect specific cycles.
