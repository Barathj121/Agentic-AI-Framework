"""CODEX-ASSISTED SHOWCASE. Reviewed descriptive analysis; run only in Docker."""
from pathlib import Path
import json
import math
import duckdb
import numpy as np
import pandas as pd
import plotly.graph_objects as go

assert Path('/.dockerenv').exists(), 'Run this analysis only in the approved Docker sandbox'
con = duckdb.connect('database/workspace.duckdb')
df = con.execute('SELECT * FROM raw.source ORDER BY transaction_timestamp, transaction_id').df()
assert len(df) == 10000 and df.transaction_id.is_unique
df['timestamp'] = pd.to_datetime(df.transaction_timestamp, utc=True)
df['hour'] = df.timestamp.dt.floor('h')
df['day'] = df.timestamp.dt.floor('D')
df['transaction_amount'] = pd.to_numeric(df.transaction_amount)
assert df[['customer_id', 'account_id', 'destination_account', 'timestamp', 'transaction_amount']].notna().all().all()
assert (df.transaction_amount >= 0).all()
transfers = df[df.transaction_type.eq('transfer')].copy()
hourly = transfers.groupby(['customer_id', 'hour']).size().rename('hourly_transfers').reset_index()
recipients = transfers.groupby('destination_account').agg(source_accounts=('account_id', 'nunique'), customers=('customer_id', 'nunique'), transfers=('transaction_id', 'size'), total_amount=('transaction_amount', 'sum')).reset_index()
thresholds = {
    'burst_min_transfers_per_clock_hour': max(5, int(math.ceil(hourly.hourly_transfers.quantile(.99)))),
    'modest_transfer_max_amount': round(float(transfers.transaction_amount.quantile(.75)), 2),
    'repeat_min_transfers_same_recipient_clock_hour': 5,
    'repeat_max_coefficient_of_variation': .15,
    'large_amount_customer_median_multiple': 10,
    'large_amount_global_floor': round(float(df.transaction_amount.quantile(.99)), 2),
    'hub_min_distinct_source_accounts': max(10, int(math.ceil(recipients.source_accounts.quantile(.99)))),
    'behavior_late_early_median_multiple': 5,
    'behavior_min_transactions_each_half': 5,
}
customer = df.groupby('customer_id').agg(transaction_count=('transaction_id', 'size'), total_amount=('transaction_amount', 'sum'), median_amount=('transaction_amount', 'median'), max_amount=('transaction_amount', 'max')).reset_index()
df = df.merge(customer[['customer_id', 'median_amount']], on='customer_id', how='left')
df['amount_baseline_multiple'] = df.transaction_amount / df.median_amount.clip(lower=.01)
df['large_amount_signal'] = (df.amount_baseline_multiple >= 10) & (df.transaction_amount >= thresholds['large_amount_global_floor'])
df = df.merge(hourly, on=['customer_id', 'hour'], how='left')
df['hourly_transfers'] = df.hourly_transfers.fillna(0).astype(int)
df['burst_signal'] = df.transaction_type.eq('transfer') & (df.hourly_transfers >= thresholds['burst_min_transfers_per_clock_hour'])
modest = transfers[transfers.transaction_amount <= thresholds['modest_transfer_max_amount']]
repeat = modest.groupby(['customer_id', 'destination_account', 'hour']).agg(repeated_transfer_count=('transaction_id', 'size'), repeat_mean=('transaction_amount', 'mean'), repeat_std=('transaction_amount', 'std')).reset_index()
repeat['repeat_cv'] = repeat.repeat_std.fillna(0) / repeat.repeat_mean.clip(lower=.01)
repeat['repeat_group_signal'] = (repeat.repeated_transfer_count >= 5) & (repeat.repeat_cv <= .15)
df = df.merge(repeat[['customer_id', 'destination_account', 'hour', 'repeated_transfer_count', 'repeat_cv', 'repeat_group_signal']], on=['customer_id', 'destination_account', 'hour'], how='left')
df['repeated_transfer_count'] = df.repeated_transfer_count.fillna(0).astype(int)
df['repeated_modest_signal'] = df.repeat_group_signal.fillna(False).astype(bool) & df.transaction_type.eq('transfer') & (df.transaction_amount <= thresholds['modest_transfer_max_amount'])
df = df.merge(recipients[['destination_account', 'source_accounts']], on='destination_account', how='left')
df['source_accounts'] = df.source_accounts.fillna(0).astype(int)
df['shared_recipient_signal'] = df.transaction_type.eq('transfer') & (df.source_accounts >= thresholds['hub_min_distinct_source_accounts'])
# Per-customer chronological halves: descriptive shift, not a predictive baseline.
shift_records = []
for cid, group in df.sort_values(['timestamp', 'transaction_id']).groupby('customer_id'):
    half = len(group) // 2
    early, late = group.iloc[:half], group.iloc[half:]
    ratio = float(late.transaction_amount.median() / max(.01, early.transaction_amount.median())) if half else 1.0
    shift_records.append({'customer_id': cid, 'early_median': float(early.transaction_amount.median()) if half else 0.0, 'late_median': float(late.transaction_amount.median()), 'behavior_ratio': ratio, 'shift_signal': half >= 5 and ratio >= 5})
shift = pd.DataFrame(shift_records)
df = df.merge(shift[['customer_id', 'behavior_ratio', 'shift_signal']], on='customer_id')
signals = ['burst_signal', 'repeated_modest_signal', 'large_amount_signal', 'shared_recipient_signal', 'shift_signal']
friendly = {'burst_signal': 'Hourly transfer burst', 'repeated_modest_signal': 'Repeated modest transfers', 'large_amount_signal': 'Large relative to own median', 'shared_recipient_signal': 'Shared recipient', 'shift_signal': 'Spending behavior change'}
customer_signals = df.groupby('customer_id')[signals].max().reset_index()
features = df.groupby('customer_id').agg(max_hourly_transfers=('hourly_transfers', 'max'), max_repeat_transfers=('repeated_transfer_count', 'max'), max_amount_multiple=('amount_baseline_multiple', 'max'), max_recipient_sources=('source_accounts', 'max')).reset_index()
ranking = customer.merge(customer_signals, on='customer_id').merge(features, on='customer_id').merge(shift.drop(columns=['shift_signal']), on='customer_id')
ranking['signal_count'] = ranking[signals].sum(axis=1).astype(int)
ranking['review_score'] = ranking.signal_count * 20
ranking['review_reasons'] = ranking.apply(lambda row: '; '.join(friendly[s] for s in signals if row[s]) or 'No selected signal', axis=1)
ranking = ranking.sort_values(['review_score', 'max_hourly_transfers', 'max_amount_multiple', 'customer_id'], ascending=[False, False, False, True]).reset_index(drop=True)
ranking.insert(0, 'review_rank', range(1, len(ranking) + 1))
evidence = df[df[signals].any(axis=1)].copy()
evidence['evidence_reasons'] = evidence.apply(lambda row: '; '.join(friendly[s] for s in signals if row[s]), axis=1)
evidence = evidence[['transaction_id', 'customer_id', 'account_id', 'destination_account', 'timestamp', 'transaction_amount', 'transaction_type', 'hourly_transfers', 'repeated_transfer_count', 'amount_baseline_multiple', 'source_accounts', 'behavior_ratio', 'evidence_reasons', *signals]]
edges = transfers.groupby(['account_id', 'destination_account']).agg(transfer_count=('transaction_id', 'size'), total_amount=('transaction_amount', 'sum')).reset_index()
edges = edges.merge(recipients[['destination_account', 'source_accounts']], on='destination_account')
daily = transfers.groupby(['day', 'customer_id']).agg(transfers=('transaction_id', 'size'), total_amount=('transaction_amount', 'sum')).reset_index()
sensitivities = []
for multiplier in [.8, 1, 1.2]:
    # Vary three numeric signal thresholds only. Report counts, not model accuracy.
    burst = df.transaction_type.eq('transfer') & (df.hourly_transfers >= max(3, math.ceil(thresholds['burst_min_transfers_per_clock_hour'] * multiplier)))
    large = (df.amount_baseline_multiple >= 10 * multiplier) & (df.transaction_amount >= thresholds['large_amount_global_floor'] * multiplier)
    shared = df.transaction_type.eq('transfer') & (df.source_accounts >= math.ceil(thresholds['hub_min_distinct_source_accounts'] * multiplier))
    selected = burst | large | shared | df.repeated_modest_signal | df.shift_signal
    sensitivities.append({'threshold_multiplier': multiplier, 'customers_for_review': int(df.loc[selected, 'customer_id'].nunique()), 'transactions_in_evidence': int(selected.sum())})
sensitivity = pd.DataFrame(sensitivities)
for name, data in [('customer_review', ranking), ('transaction_evidence', evidence), ('recipient_summary', recipients), ('transfer_edges', edges), ('hourly_activity', hourly), ('daily_activity', daily), ('threshold_sensitivity', sensitivity)]:
    con.register('computed_frame', data)
    con.execute(f'CREATE OR REPLACE TABLE results.{name} AS SELECT * FROM computed_frame')
    data.to_csv(f'results/{name}.csv', index=False)
    con.unregister('computed_frame')

palette = ['#00ccb4', '#6979f8', '#ffb65c', '#e879c6', '#8bc6ff']
def save_chart(figure, name):
    figure.update_layout(template='plotly_dark', paper_bgcolor='#101a2c', plot_bgcolor='#101a2c', font={'family': 'Arial', 'size': 13, 'color': '#f2f5fa'}, title_font_color='#f2f5fa', legend_font_color='#f2f5fa', margin={'l': 45, 'r': 25, 't': 70, 'b': 45}, height=480)
    figure.update_xaxes(tickfont_color='#f2f5fa', title_font_color='#f2f5fa')
    figure.update_yaxes(tickfont_color='#f2f5fa', title_font_color='#f2f5fa')
    Path('results/' + name + '.json').write_text(figure.to_json(), encoding='utf-8')
top = ranking.head(12).iloc[::-1]
fig = go.Figure()
for signal, color in zip(signals, palette):
    fig.add_bar(y=top.customer_id.tolist(), x=(top[signal].astype(int) * 20).tolist(), name=friendly[signal], orientation='h', marker_color=color)
fig.update_layout(title='Why these customers enter the review queue', barmode='stack', xaxis_title='Review points (heuristic, not probability)', legend={'orientation': 'h', 'y': -0.25})
save_chart(fig, 'review_ranking_chart')
hub = recipients.sort_values(['source_accounts', 'transfers'], ascending=False).iloc[0]
hub_edges = edges[edges.destination_account.eq(hub.destination_account)].sort_values('transfer_count', ascending=False)
labels = hub_edges.account_id.tolist() + [hub.destination_account]
fig = go.Figure(go.Sankey(node={'label': labels, 'pad': 10, 'thickness': 16, 'color': ['#6979f8'] * len(hub_edges) + ['#00ccb4']}, link={'source': list(range(len(hub_edges))), 'target': [len(hub_edges)] * len(hub_edges), 'value': hub_edges.transfer_count.tolist(), 'customdata': hub_edges.total_amount.round(2).tolist(), 'hovertemplate': '%{source.label} → %{target.label}<br>%{value} transfers<br>Amount %{customdata}<extra></extra>', 'color': 'rgba(105,121,248,.35)'}))
fig.update_layout(title=f'Shared recipient {hub.destination_account}: {int(hub.source_accounts)} source accounts', font_size=11)
save_chart(fig, 'recipient_flow_chart')
fig = go.Figure()
burst_customers = ranking.sort_values('max_hourly_transfers', ascending=False).head(6).customer_id.tolist()
for index, cid in enumerate(burst_customers):
    history = daily[daily.customer_id.eq(cid)]
    fig.add_scatter(x=history.day.astype(str).tolist(), y=history.transfers.tolist(), mode='lines+markers', name=cid, line={'color': palette[index % len(palette)]})
fig.update_layout(title='When activity concentrates: daily transfer timeline', xaxis_title='UTC date', yaxis_title='Transfers per day')
save_chart(fig, 'activity_timeline_chart')
signal_counts = [int(ranking[s].sum()) for s in signals]
fig = go.Figure(go.Bar(x=[friendly[s] for s in signals], y=signal_counts, marker_color=palette, text=signal_counts, textposition='outside'))
fig.update_layout(title='Different signals reveal different review cases', yaxis_title='Customers meeting each rule (may overlap)')
save_chart(fig, 'signal_coverage_chart')

top_cases = []
for row in ranking.head(12).itertuples(index=False):
    tx = evidence[evidence.customer_id.eq(row.customer_id)].sort_values(['hourly_transfers', 'transaction_amount'], ascending=False)
    top_cases.append({'customer_id': row.customer_id, 'review_rank': int(row.review_rank), 'review_score': int(row.review_score), 'signal_count': int(row.signal_count), 'reasons': row.review_reasons, 'max_hourly_transfers': int(row.max_hourly_transfers), 'max_amount_multiple': round(float(row.max_amount_multiple), 2), 'evidence_transaction_count': len(tx), 'sample_transaction_ids': tx.transaction_id.head(6).tolist()})
peak = hourly.sort_values('hourly_transfers', ascending=False).iloc[0]
repeat_groups = repeat[repeat.repeat_group_signal]
shift_peak = shift.sort_values('behavior_ratio', ascending=False).iloc[0]
findings = [
    {'title': 'A concentrated burst', 'detail': f'{peak.customer_id} made {int(peak.hourly_transfers)} transfers in the clock hour starting {peak.hour}.', 'query': 'SELECT * FROM results.hourly_activity ORDER BY hourly_transfers DESC LIMIT 1'},
    {'title': 'Many sources, one recipient', 'detail': f'{hub.destination_account} received {int(hub.transfers)} transfers from {int(hub.source_accounts)} distinct source accounts, totaling {float(hub.total_amount):,.2f} synthetic amount units.', 'query': 'SELECT * FROM results.recipient_summary ORDER BY source_accounts DESC, transfers DESC LIMIT 1'},
    {'title': 'Repeated modest payments', 'detail': f'{repeat_groups.customer_id.nunique()} customers had at least 5 similar modest transfers to the same recipient in a clock hour; {len(repeat_groups)} groups met the count and variation rules.', 'query': 'SELECT customer_id, max(repeated_transfer_count) AS repeat_count FROM results.transaction_evidence WHERE repeated_modest_signal GROUP BY customer_id ORDER BY repeat_count DESC'},
    {'title': 'A shift in spending', 'detail': f'{shift_peak.customer_id} has a late-half median of {shift_peak.late_median:,.2f} versus early-half {shift_peak.early_median:,.2f} ({shift_peak.behavior_ratio:.1f}×).', 'query': 'SELECT customer_id, early_median, late_median, behavior_ratio FROM results.customer_review ORDER BY behavior_ratio DESC LIMIT 1'},
]
summary = {'title': 'CODEX-ASSISTED SHOWCASE · Transaction Investigation', 'provenance': {'analysis_author': 'Codex assistant, explicitly reviewed showcase', 'local_llm_used': False, 'execution': 'Real isolated Docker sandbox', 'synthetic_data': True, 'annotation_file_used': False}, 'metrics': {'transactions': len(df), 'customers': len(ranking), 'transfers': len(transfers), 'customers_for_review': int((ranking.signal_count > 0).sum()), 'evidence_transactions': len(evidence), 'recipient_accounts': len(recipients), 'analysis_period_start': str(df.timestamp.min()), 'analysis_period_end': str(df.timestamp.max())}, 'thresholds': thresholds, 'top_cases': top_cases, 'findings': findings, 'sensitivity': sensitivities, 'limitations': ['Review scores are transparent rule counts, not probabilities, fraud labels or financial advice.', 'All data is synthetic; no accuracy or real-world crime detection claim is made.', 'Amounts are assumed to share a synthetic unit; no currency conversion is available.', 'Clock-hour boundaries can split bursts; shared recipients can have legitimate explanations.', 'Customer medians and chronological halves use the same observation period; this is retrospective description, not prospective prediction.', 'Thresholds are illustrative; sensitivity varies burst, amount and hub thresholds only. Repeated-payment and behavior rules are unchanged.', 'The generated solution notebook records module execution; notebook cells themselves were not run.']}
Path('results/showcase_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
dashboard = {'schema': 'workbench.dashboard.v1', 'title': 'Transaction Investigation', 'summary': 'From 10,000 synthetic transactions to an explainable review queue, linked evidence and shared-recipient flows.', 'provenance': 'CODEX-ASSISTED SHOWCASE. Reviewed assistant-authored analysis, executed in the real Docker sandbox. No local LLM inference. Synthetic data; review rules are not fraud probabilities.', 'metrics': [{'label': 'Transactions analyzed', 'value': f'{len(df):,}', 'help': 'Actual raw input rows; unique transaction IDs validated.'}, {'label': 'Customers', 'value': f'{len(ranking):,}', 'help': 'Every input customer is represented in the ranked results.'}, {'label': 'Customers for review', 'value': str(int((ranking.signal_count > 0).sum())), 'help': 'Customers meeting at least one descriptive rule. This is not a fraud count.'}, {'label': 'Evidence transactions', 'value': f'{len(evidence):,}', 'help': 'Supporting transaction IDs retained and validated against the original input.'}], 'findings': [item['detail'] for item in findings], 'charts': ['results/review_ranking_chart.json', 'results/recipient_flow_chart.json', 'results/activity_timeline_chart.json', 'results/signal_coverage_chart.json']}
Path('results/dashboard.json').write_text(json.dumps(dashboard, indent=2), encoding='utf-8')
report = '# CODEX-ASSISTED SHOWCASE\n\nReviewed analysis, executed in the real Docker sandbox. No local LLM inference was used.\n\n'
report += '\n\n'.join('## ' + item['title'] + '\n\n' + item['detail'] + '\n\n```sql\n' + item['query'] + '\n```' for item in findings)
report += '\n\n## Interpretation limits\n\n' + '\n'.join('- ' + item for item in summary['limitations'])
Path('results/investigation_brief.md').write_text(report, encoding='utf-8')
print(json.dumps({'metrics': summary['metrics'], 'findings': findings, 'sensitivity': sensitivities}, indent=2))
con.close()
