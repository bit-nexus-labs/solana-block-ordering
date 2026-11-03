# Whole blocks of the scheduled leaders

794 blocks from 130 validators and 10 clients, 335133 transactions (335132 non-vote).

`higher_fee_first`: among non-vote transactions in one block with different compute-unit prices, the share of pairs where the higher price is placed earlier (0.5 = no relation). Interval: 95%, resampling whole validators. `tips_in_first_quarter`: share of transactions that pay a Jito tip that sit in the first quarter of the block.

All clients: higher_fee_first = 0.50 (0.50–0.51).

| client             |   validators |   blocks |   median_tx |   vote_share |   paying_share | higher_fee_first   |   spearman_pos_price |   tipped_share |   tips_in_first_quarter |   probe_fee_agree |
|:-------------------|-------------:|---------:|------------:|-------------:|---------------:|:-------------------|---------------------:|---------------:|------------------------:|------------------:|
| Rakurai            |            9 |      102 |         369 |            0 |           0.37 | 0.49 (0.48–0.50)   |                 0.02 |          0.032 |                    0.21 |              0.69 |
| Agave              |           12 |      100 |         344 |            0 |           0.4  | 0.50 (0.49–0.51)   |                 0    |          0.002 |                    0.31 |              0.52 |
| HarmonicAgave      |           20 |      100 |         368 |            0 |           0.35 | 0.49 (0.49–0.50)   |                 0.01 |          0.032 |                    0.22 |              0.67 |
| Firedancer         |           14 |       98 |         442 |            0 |           0.34 | 0.48 (0.47–0.49)   |                 0.03 |          0.046 |                    0.24 |              0.7  |
| HarmonicFiredancer |           15 |       96 |         384 |            0 |           0.34 | 0.49 (0.49–0.50)   |                 0.01 |          0.035 |                    0.24 |              0.59 |
| AgaveBam           |           28 |       92 |         396 |            0 |           0.42 | 0.52 (0.51–0.53)   |                -0.04 |          0.037 |                    0.17 |              0.73 |
| FireBAM            |           16 |       92 |         411 |            0 |           0.41 | 0.53 (0.52–0.54)   |                -0.05 |          0.047 |                    0.14 |              0.73 |
| JitoLabs           |           15 |       88 |         351 |            0 |           0.36 | 0.50 (0.49–0.51)   |                 0.01 |          0.065 |                    0.24 |              0.57 |
| Raiku              |            1 |       14 |         351 |            0 |           0.36 | 0.47               |                 0.06 |          0.016 |                    0.1  |            nan    |
| Unknown            |            1 |       12 |         204 |            0 |           0.33 | 0.52               |                -0.03 |          0.003 |                    0.13 |              0.3  |

## By session

| session               |   blocks |   median_tx |   higher_fee_first |   paying_share |
|:----------------------|---------:|------------:|-------------------:|---------------:|
| 20261102T003610Z-live |       44 |       322.5 |               0.49 |           0.34 |
| 20261102T053618Z-live |      126 |       226.5 |               0.5  |           0.39 |
| 20261102T071208Z-live |      132 |       247.5 |               0.5  |           0.44 |
| 20261102T143037Z-live |      126 |       560.5 |               0.5  |           0.29 |
| 20261103T153045Z-live |      188 |       419.5 |               0.5  |           0.35 |
| 20261103T192233Z-live |      178 |       448   |               0.5  |           0.4  |
