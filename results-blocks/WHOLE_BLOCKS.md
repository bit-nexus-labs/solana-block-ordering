# Whole blocks of the scheduled leaders

794 blocks from 130 validators and 10 clients, 335133 transactions. The RPC's full-block answer did not include vote transactions, so everything below is about non-vote transactions only.

* `top_fee_shift`: mean position (0 = first, 1 = last) of transactions paying at least 100,000 micro-lamports per compute unit, minus that of all other transactions in the same block. Negative = expensive transactions sit earlier.
* `higher_fee_first_near`: pairs at most 10 positions apart with different prices; share where the higher price comes first (0.5 = no relation). `higher_fee_first_all`: the same over all pairs in the block.
* `probe_fee_agree`: the same share for our own probe transactions, which were sent together (from `analyze.py`, same-block pairs, clients with at least 20 pairs).
* Intervals: 95%, resampling whole validators.

All clients: top_fee_shift = -0.034 (-0.040 … -0.028), higher_fee_first_near = 0.51 (0.51 … 0.51), failed transactions 34%, paying a priority fee 37%.

## By client

| client             |   validators |   blocks |   median_tx |   paying_share |   failed_share |   top_fee_position | top_fee_shift            |   higher_fee_first_all | higher_fee_first_near   |   jito_tipped_share |   probe_fee_agree |
|:-------------------|-------------:|---------:|------------:|---------------:|---------------:|-------------------:|:-------------------------|-----------------------:|:------------------------|--------------------:|------------------:|
| Rakurai            |            9 |      102 |         369 |           0.37 |           0.35 |              0.477 | -0.025 (-0.039 … -0.010) |                  0.491 | 0.50 (0.50 … 0.50)      |               0.032 |              0.69 |
| Agave              |           12 |      100 |         344 |           0.4  |           0.35 |              0.48  | -0.022 (-0.041 … -0.010) |                  0.5   | 0.50 (0.50 … 0.51)      |               0.002 |              0.52 |
| HarmonicAgave      |           20 |      100 |         368 |           0.35 |           0.32 |              0.477 | -0.026 (-0.049 … -0.008) |                  0.494 | 0.51 (0.51 … 0.52)      |               0.032 |              0.67 |
| Firedancer         |           14 |       98 |         442 |           0.34 |           0.31 |              0.462 | -0.040 (-0.057 … -0.023) |                  0.484 | 0.51 (0.50 … 0.52)      |               0.046 |              0.7  |
| HarmonicFiredancer |           15 |       96 |         384 |           0.34 |           0.32 |              0.475 | -0.027 (-0.038 … -0.017) |                  0.494 | 0.51 (0.51 … 0.52)      |               0.035 |              0.59 |
| AgaveBam           |           28 |       92 |         396 |           0.42 |           0.37 |              0.444 | -0.065 (-0.078 … -0.053) |                  0.522 | 0.52 (0.52 … 0.53)      |               0.037 |              0.73 |
| FireBAM            |           16 |       92 |         411 |           0.41 |           0.35 |              0.455 | -0.052 (-0.065 … -0.037) |                  0.527 | 0.52 (0.52 … 0.53)      |               0.047 |              0.73 |
| JitoLabs           |           15 |       88 |         351 |           0.36 |           0.35 |              0.485 | -0.016 (-0.030 … +0.005) |                  0.498 | 0.50 (0.50 … 0.50)      |               0.065 |              0.57 |
| Raiku              |            1 |       14 |         351 |           0.36 |           0.23 |              0.5   | -0.000                   |                  0.473 | 0.50                    |               0.016 |            nan    |
| Unknown            |            1 |       12 |         204 |           0.33 |           0.2  |              0.443 | -0.061                   |                  0.518 | 0.50                    |               0.003 |              0.3  |

## By compute-unit price

| cu_price (micro-lamports)   |   transactions |   share |   mean_position |   failed_share |   jito_tipped_share |
|:----------------------------|---------------:|--------:|----------------:|---------------:|--------------------:|
| 0                           |         212200 |   0.633 |           0.496 |           0.35 |               0.052 |
| 1–1k                        |          36205 |   0.108 |           0.538 |           0.48 |               0.008 |
| 1k–10k                      |          20518 |   0.061 |           0.514 |           0.4  |               0.012 |
| 10k–100k                    |          37153 |   0.111 |           0.508 |           0.53 |               0.009 |
| 100k–1M                     |          22703 |   0.068 |           0.464 |           0.23 |               0.014 |
| ≥1M                         |           6354 |   0.019 |           0.458 |           0.19 |               0.023 |

## By session

| session               |   blocks |   median_tx |   paying_share |   failed_share |   top_fee_shift |   higher_fee_first_near |
|:----------------------|---------:|------------:|---------------:|---------------:|----------------:|------------------------:|
| 20261102T003610Z-live |       44 |       322.5 |          0.337 |          0.405 |          -0.028 |                   0.505 |
| 20261102T053618Z-live |      126 |       226.5 |          0.388 |          0.229 |          -0.03  |                   0.509 |
| 20261102T071208Z-live |      132 |       247.5 |          0.443 |          0.216 |          -0.027 |                   0.509 |
| 20261102T143037Z-live |      126 |       560.5 |          0.289 |          0.383 |          -0.034 |                   0.511 |
| 20261103T153045Z-live |      188 |       419.5 |          0.351 |          0.364 |          -0.03  |                   0.509 |
| 20261103T192233Z-live |      178 |       448   |          0.396 |          0.417 |          -0.047 |                   0.516 |
