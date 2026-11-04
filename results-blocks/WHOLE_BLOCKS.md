# Whole blocks of the scheduled leaders

1126 blocks from 153 validators and 10 clients, 444350 transactions. The RPC's full-block answer did not include vote transactions, so everything below is about non-vote transactions only.

* `top_fee_shift`: mean position (0 = first, 1 = last) of transactions paying at least 100,000 micro-lamports per compute unit, minus that of all other transactions in the same block. Negative = expensive transactions sit earlier.
* `higher_fee_first_near`: pairs at most 10 positions apart with different prices; share where the higher price comes first (0.5 = no relation). `higher_fee_first_all`: the same over all pairs in the block.
* `probe_fee_agree`: the same share for our own probe transactions, which were sent together (from `analyze.py`, same-block pairs, clients with at least 20 pairs).
* Intervals: 95%, resampling whole validators.

All clients: top_fee_shift = -0.031 (-0.037 … -0.026), higher_fee_first_near = 0.51 (0.51 … 0.51), failed transactions 32%, paying a priority fee 38%.

## By client

| client             |   validators |   blocks |   median_tx |   paying_share |   failed_share |   top_fee_position | top_fee_shift            |   higher_fee_first_all | higher_fee_first_near   |   jito_tipped_share |   probe_fee_agree |
|:-------------------|-------------:|---------:|------------:|---------------:|---------------:|-------------------:|:-------------------------|-----------------------:|:------------------------|--------------------:|------------------:|
| Firedancer         |           16 |      144 |         396 |           0.34 |           0.29 |              0.473 | -0.029 (-0.044 … -0.011) |                  0.481 | 0.51 (0.51 … 0.52)      |               0.044 |              0.71 |
| Agave              |           15 |      140 |         292 |           0.39 |           0.32 |              0.487 | -0.014 (-0.029 … -0.002) |                  0.496 | 0.50 (0.50 … 0.51)      |               0.002 |              0.55 |
| HarmonicAgave      |           22 |      140 |         352 |           0.35 |           0.31 |              0.476 | -0.026 (-0.043 … -0.011) |                  0.491 | 0.51 (0.51 … 0.51)      |               0.032 |              0.69 |
| Rakurai            |            9 |      138 |         327 |           0.37 |           0.33 |              0.475 | -0.027 (-0.041 … -0.014) |                  0.495 | 0.50 (0.50 … 0.50)      |               0.03  |              0.7  |
| FireBAM            |           17 |      138 |         360 |           0.41 |           0.32 |              0.455 | -0.053 (-0.066 … -0.038) |                  0.526 | 0.52 (0.52 … 0.52)      |               0.047 |              0.74 |
| HarmonicFiredancer |           19 |      138 |         359 |           0.34 |           0.3  |              0.475 | -0.028 (-0.041 … -0.016) |                  0.494 | 0.51 (0.51 … 0.52)      |               0.034 |              0.64 |
| AgaveBam           |           36 |      136 |         369 |           0.43 |           0.33 |              0.451 | -0.057 (-0.068 … -0.046) |                  0.518 | 0.52 (0.52 … 0.53)      |               0.041 |              0.74 |
| JitoLabs           |           18 |      126 |         333 |           0.38 |           0.35 |              0.485 | -0.016 (-0.028 … -0.003) |                  0.498 | 0.50 (0.50 … 0.50)      |               0.065 |              0.62 |
| Raiku              |            1 |       14 |         351 |           0.36 |           0.23 |              0.5   | -0.000                   |                  0.473 | 0.50                    |               0.016 |            nan    |
| Unknown            |            1 |       12 |         204 |           0.33 |           0.2  |              0.443 | -0.061                   |                  0.518 | 0.50                    |               0.003 |              0.3  |

## By compute-unit price

| cu_price (micro-lamports)   |   transactions |   share |   mean_position |   failed_share |   jito_tipped_share |
|:----------------------------|---------------:|--------:|----------------:|---------------:|--------------------:|
| 0                           |         278819 |   0.627 |           0.495 |           0.32 |               0.052 |
| 1–1k                        |          49311 |   0.111 |           0.536 |           0.5  |               0.007 |
| 1k–10k                      |          27245 |   0.061 |           0.514 |           0.36 |               0.013 |
| 10k–100k                    |          48150 |   0.108 |           0.511 |           0.51 |               0.009 |
| 100k–1M                     |          32620 |   0.073 |           0.468 |           0.23 |               0.011 |
| ≥1M                         |           8205 |   0.018 |           0.461 |           0.19 |               0.022 |

## By session

| session               |   blocks |   median_tx |   paying_share |   failed_share |   top_fee_shift |   higher_fee_first_near |
|:----------------------|---------:|------------:|---------------:|---------------:|----------------:|------------------------:|
| 20261102T003610Z-live |       44 |       322.5 |          0.337 |          0.405 |          -0.028 |                   0.505 |
| 20261102T053618Z-live |      126 |       226.5 |          0.388 |          0.229 |          -0.03  |                   0.509 |
| 20261102T071208Z-live |      132 |       247.5 |          0.443 |          0.216 |          -0.027 |                   0.509 |
| 20261102T143037Z-live |      126 |       560.5 |          0.289 |          0.383 |          -0.034 |                   0.511 |
| 20261103T153045Z-live |      188 |       419.5 |          0.351 |          0.364 |          -0.03  |                   0.509 |
| 20261103T192233Z-live |      178 |       448   |          0.396 |          0.417 |          -0.047 |                   0.516 |
| 20261104T080046Z-live |      332 |       297.5 |          0.387 |          0.272 |          -0.025 |                   0.511 |
