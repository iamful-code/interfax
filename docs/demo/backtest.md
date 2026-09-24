## Метрики стратегии

| metric | value |
|---|---|
| n_days | 699 |
| total_return | 0.1082 |
| cagr | 0.0377 |
| annual_vol | 0.0173 |
| sharpe | 2.1521 |
| sortino | 3.8576 |
| max_drawdown | -0.0125 |
| calmar | 3.0269 |
| n_trades | 54 |
| hit_rate | 0.7222 |
| avg_win | 0.069 |
| avg_loss | -0.0415 |
| profit_factor | 4.3659 |
| avg_hold_days | 10 |
| avg_net_ret | 0.0383 |
| turnover | 1.9683 |
| total_commission | 2861.0174 |
| exit_reasons | {'hold_days': 54} |
| alpha_daily | 0.0002 |
| alpha_annual | 0.0387 |
| alpha_t | 3.8972 |
| beta | 0.0405 |
| information_ratio | 0.4057 |
| index_total_return | -0.1423 |
| exposure_avg | 0.0391 |
| n_signals | 55 |
| skipped_signals | 1 |
| skipped_by_reason | {'already_held': 1} |

## Плацебо: та же стратегия на случайных датах

| metric | real | placebo_mean | placebo_p5 | placebo_p95 | percentile_of_real | n_iter |
|---|---:|---:|---:|---:|---:|---:|
| sharpe | 2.1521 | -0.0069 | -0.914 | 0.6709 | 1 | 50 |
| total_return | 0.1082 | -0.0002 | -0.0389 | 0.0299 | 1 | 50 |

## Walk-forward (разбиение 2022-04-20, лучший набор {'hold_days': 20})

| metric | in_sample | out_of_sample |
|---|---:|---:|
| total_return | 0.0374 | 0.13 |
| cagr | 0.0318 | 0.0774 |
| sharpe | 0.5961 | 1.7422 |
| max_drawdown | -0.0658 | -0.0275 |
| hit_rate | 0.5789 | 0.697 |
| n_trades | 76 | 66 |
| alpha_annual | 0.0435 | 0.0722 |
| alpha_t | 1.2879 | 2.7288 |
| beta | 0.2189 | 0.1478 |
