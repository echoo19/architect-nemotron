## receipts

| Arm | Model | Delivery | 95% CI | Invalid | Over budget | Exam | Licensed | $/run | p50 s | p95 s | Calls/run | Build $ | Tuning, exam, delivery |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A1 | nvidia/Nemotron-3-Ultra-550b-a55b | 9/10 (0.900) | [0.596, 0.982] | 0 | 1 | 96/100 | yes | $0.002100 | 2.10 | 4.50 | 1.00 | $0.2500 | 0.970 / 0.960 / 0.900 |
| A3 | nvidia/Nemotron-3_5-Lightning | 6/10 (0.600) | [0.313, 0.832] | 1 | 0 | 85/100 | no | $0.000220 | 0.30 | 0.91 | 1.00 | $0.0200 | 0.880 / 0.850 / 0.600 |
| A7 | nvidia/Nemotron-3_5-Lightning | 8/10 (0.800) | [0.490, 0.943] | 0 | 0 | 93/100 | yes | $0.000100 | 0.06 | 0.40 | 0.80 | $1.2655 | 0.990 / 0.930 / 0.800 |

## Paired arms: receipts

| Base | Candidate | n | Diff | 95% CI | McNemar p | Holm p | Non-inferior (0.05) | Cost ratio | 95% CI |
|---|---|---|---|---|---|---|---|---|---|
| A3 | A7 | 10 | +0.200 | [-0.049, +0.429] | 0.5000 | 1.000 | yes | 0.455 | [0.286, 0.583] |
| A1 | A7 | 10 | -0.100 | [-0.362, +0.134] | 1.000 | 1.000 | no | 0.048 | [0.030, 0.064] |

Suite v2 5ca1ab1e; settings frozen: yes.

Evidence: live_billed. Floor origin: public_dataset.

Delivery cases fff4adda, n = 10.
