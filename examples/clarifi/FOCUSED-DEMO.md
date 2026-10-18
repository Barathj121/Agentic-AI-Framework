# Focused ClariFi demonstration

These 13 records are entirely synthetic, curated positive scenarios for a short
functional demonstration. They are not actual trades or companies and do not
measure detection accuracy, precision, recall or fraud probability.

Use **ClariFi Agent → Combined investigation → Focused high-priority example**,
or upload `focused_review.xlsx` using **Upload Excel workbook**. The matching
`focused_review.json` is also available. For migration, choose **Prepare with
Migration**, provide the workbook or JSON, review each table's proposed mappings,
convert in Docker, review the converted data and approve investigation. A model's
mapping suggestion may need correction; never approve an ambiguous identifier.

The current rules baseline is:

| Review lead | Expected triage score |
|---|---:|
| Employee's linked account trades before a supplied event, with issuer access | 95 |
| Declared spouse account trades before the same event, with employee issuer access | 95 |
| Three companies in one chronological payment cycle, sharing an address and bank account | 80 each |

There are five findings: two trading leads and three company leads from **one**
payment cycle. These findings warrant analyst review and do not establish
wrongdoing. Gemini/Gemma plans and explains the investigation; verified tool
contributions determine scores. AI wording may vary by model.

Results default to **High-priority review**: disposition `analyst_review` and score
at least 80/100. **All findings** reveals other leads without rerunning the model.
Full JSON reports retain all findings, regardless of the display filter.

For comparison cases, use **Synthetic example**, the existing larger workbook or
separate adjudicated evaluation data. Do not treat this positive demo as a balanced
test set or use it to claim an 80–90% detection accuracy.
