# Input data bundle — AI Agentic Engineer assignment

This is the realistic, intentionally messy mock data referenced in the assignment PDF.
**Do not clean it before processing — handling the mess is the work.**

## Files

| File | Description |
|---|---|
| `chart_of_accounts.csv` | Hierarchical COA: account code, name, type, parent, statement (BS/PL), cash-flow category, normal balance |
| `trial_balance.csv` | Period-end TB for fictional entity. Multi-currency rows where applicable. Columns: `account_code, account_name, currency, debit, credit` |
| `prior_period_tb.csv` | Prior-period TB for opening balances and comparatives |
| `manual_adjustments.json` | 10 journal entries from the finance team (accruals, reclasses, FX reval, intercompany) |
| `fx_rates.csv` | Period-average and period-end FX rates for non-functional currencies |

## Conventions

- Functional currency: **USD**
- Period: **2024-Q4**
- Normal balance: assets/expenses are debits, liabilities/equity/revenue are credits
- TB rows can repeat the same `account_code` across currencies — those should be summed in the functional currency after FX translation
- Adjustments JSON is an unposted batch — your system decides whether to accept, reject, or quarantine each entry

## What we are NOT telling you

The assignment PDF lists some seeded defects in the inputs. There are more than the PDF states. Finding them is part of the evaluation.

## How we suggest you start

1. Run `wc -l` on the CSVs and skim them to get a feel for the shape
2. Eyeball the JSON to see what an adjustment looks like
3. Check if total debits equals total credits in the TB (it does not — figure out why)
4. Then go read the assignment PDF again and pick your slice
