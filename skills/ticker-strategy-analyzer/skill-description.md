# Skill: ticker-strategy-analyzer

This skill helps the agent evaluate stock movement signals.
It defines how to rate factors from 0 to 5.
It defines how to classify movement direction.
It defines how to build scenario maps.
It defines how to produce ASD-STE100 compliant text.

It also defines how to match a stock against 8 short-term strategies and how
to score the confidence of the best fit. The code in
`src/ticker_review/strategies.py` and `src/ticker_review/agents/strategy_analyzer.py`
applies these rules. If you change a rule here, change the code too.

## Core principle

Most tickers do not have a valid setup on a given day. "No setup" is the
correct and most common answer. A confidence below 50 means no trade. Do not
force a strategy onto a stock. A missed trade costs nothing. A bad entry
costs money.

## 1. Factor ratings (0 to 5)

Rate each of the 9 research factors. 0 means no observed data. 5 means a
strong, clear signal.

| Factor | Rule |
|---|---|
| Trading volume and liquidity | From relative volume (last session vs 50-session average). Change below 20% = 1, below 50% = 2, below 100% = 3, below 200% = 4, else 5. |
| Industry sector trends | From the gap between the sector ETF and SPY over 1 month. Below 1 pt = 1, below 3 = 2, below 5 = 3, below 8 = 4, else 5. |
| Corporate actions | Earnings within 5 sessions (before or after) = 4. Earnings within 10 sessions = 3. Else use the news rule. |
| All news factors | No item = 0. One unknown source = 1. One semi-trusted source = 2. One trusted source = 3. Two or more trusted sources = 4. Add 1 (max 5) when the news gives a clear direction. If the topic is found but the direction is not classified (no model), cap at 2. |

Each rating has a direction: positive, negative, neutral or no data.

## 2. Movement direction (Rise / Fall / Sideways)

Count votes. Each vote is +1, -1 or 0.

- 5-session change above +1% = +1. Below -1% = -1.
- Close above the 21 EMA = +1. Below = -1.
- MACD histogram above 0 = +1. Below 0 = -1.
- News: more positive than negative items = +1. More negative = -1.

The research agent uses these 4 votes. The strategy analyzer adds:

- Market regime: strong or cautious bull = +1. Bear or panic = -1.
- Best-fit strategy confidence 65 or more = +1.

Total of +2 or more = Rise. Total of -2 or less = Fall. Else = Sideways.

Movement confidence:
- High: absolute total 4 or more, and 7 or more of 9 factors have data.
- Medium: absolute total 2 or more, and 5 or more factors have data.
- Low: all other cases.

## 3. Scenario map (next 5 trading sessions = 7 calendar days)

Use the ATR(14) range. Range = ATR(14) x square root of 5.

- Best case: price goes up to close + range.
- Worst case: price goes down to close - range.
- Expected case: Rise = close - 0.25 range to close + 0.75 range.
  Fall = close - 0.75 range to close + 0.25 range.
  Sideways = close +/- 0.5 range.
- Name the nearest support (50 EMA, 200 EMA) and resistance (60-session high,
  52-week high) that are inside each range.

This is a volatility range. It is not a forecast.

## 4. Risk profile

| Risk | Low | Medium | High |
|---|---|---|---|
| Market | Strong bull regime | Cautious bull or transition | Bear or panic |
| Sector | Sector ETF beats SPY by 1 pt or more (1 month) | Between | Sector ETF trails SPY by 3 pt or more |
| Liquidity | 20-day avg volume 1M+ and turnover $50M+ | Avg volume 300K+ | Below 300K |
| Event | No earnings in next 20 sessions | Earnings in 11-20 sessions or date unknown | Earnings in next 10 sessions or in last 2 sessions |

## 5. Market regime gate (applies to all strategies)

| S&P 500 vs 200-DMA | VIX | Regime | Effect |
|---|---|---|---|
| Above, by 5%+ | Below 18 | Strong bull | All strategies valid |
| Above | 18-25 | Cautious bull | Reduce size 25% |
| Near (+/-3%) | 20-28 | Transition | Only PEAD and Mean Reversion |
| Below | Above 25 | Bear | Mean Reversion only |
| Below, by 5%+ | Above 35 | Panic | No new entries |

Long-only strategies (VCP, Momentum, 52-Week High, Gap and Go, Sector
Rotation) never score above 50 when the S&P 500 is below its 200-DMA.

## 6. The 8 strategies

A gate that "nearly" passes is a fail. RSI 41 does not pass "RSI below 40".
A gate with no data is a fail.

### 6.1 Mean Reversion
Mandatory: RSI(14) < 40; price > 200 EMA; price < 50 EMA; 1-month perf < -5%;
P/E > 1; market cap > $2B, price > $25, avg volume > 300K.
Optional: RSI < 32; price < lower Bollinger Band (20, 2); ADX(14) < 25;
bullish rejection candle; volume spike on a down day; revenue growth > 5%.
Disqualifier: drop caused by an earnings miss (miss in the last 21 sessions).
Hold 3-15 days. Stop = entry - 1.5 x ATR(14). T1 = 20 SMA. T2 = 50 EMA.

### 6.2 PEAD
Mandatory: earnings in the last 3 trading days; EPS beat > 5%; stock rose on
the earnings day; earnings-day volume 2x+ the 10-day average; price > 50 EMA.
Optional: EPS beat > 10%; EPS beat > 20%; 52-week high in the last 3 months;
revenue growth > 0.
Hold 5-60 days. Stop = -4%. T1 = +8%. T2 = +15%.

### 6.3 VCP Breakout
Mandatory: S&P 500 above its 50-DMA; stage 2 (price > 50 > 150 > 200 EMA);
200 EMA rising for 1 month; 2+ contractions, each smaller; volume dry-up in
the last contraction; breakout volume 40%+ above the 50-day average with a
close above the prior 20-session high; within 25% of the 52-week high; 30%+
above the 52-week low.
Optional: ATR(14) at or below 1/3 of its 50-session average;
3+ contractions; outperforming the S&P 500 over 3 months.
Hold 10-30 days. Stop = below the last contraction low, max 8%. T1 = 2R. T2 = 3R.
Contractions are approximated from daily bars in three 20-session windows.

### 6.4 Gap and Go
Mandatory: opening gap > 3%; volume 2x+ average; a real catalyst (earnings in
the last 2 sessions or a dated news item from the last 2 days); price > 200
EMA; RSI(14) 40-70; market cap > $2B, price > $15.
Optional: gap > 5% with volume 3x+; close above the open; no 60-session high
within 5% above.
Disqualifiers: gap > 15%; gap on an earnings miss.
Hold 1-3 days. Stop = below the gap open. T1 = +4.5%. T2 = prior resistance
(60-session high).

### 6.5 Momentum / Trend Following
Mandatory: 3-month perf > 15%; 1-month perf > 5%; price > 50 EMA and > 200
EMA; RSI(14) 50-70; avg volume > 500K; market cap > $2B.
Optional: beats S&P 500 over 3 and 6 months; ADX(14) > 20; within 2% of the
21 EMA (pullback entry); last EPS surprise > 0; sector ETF beats SPY (1 month).
Disqualifier: RSI > 78.
Hold 20-60 days. Stop = 21 EMA. T1 = prior swing high (60-session high).
T2 = analyst consensus.

### 6.6 Earnings Run-Up
Mandatory: earnings 5-10 trading days away; EPS beat in 3+ of the last 4
quarters; price > 200 EMA; RSI(14) 45-65; avg volume > 300K, market cap > $2B.
Optional: beat 4 of 4; average beat > 5%; analyst rating buy or strong buy.
Rule: sell the day before earnings, always.
Hold 3-10 days. Stop = -3%. T1 = +6.5%. T2 = exit the day before earnings.

### 6.7 52-Week High Breakout
Mandatory: within 2% of the 52-week high; relative volume > 1.5x; price > 200
EMA; RSI(14) 55-75; 1-month perf > 8%; market cap > $2B, price > $25.
Optional: prior 52-week high held for 6+ months; ADX > 20; earnings growth > 0;
earnings more than 5 days away.
Disqualifier: earnings within 5 days.
Hold 20-90 days. Stop = -4%. T1 = +10%. T2 = analyst consensus.

### 6.8 Sector Rotation
Mandatory: sector ETF beats SPY over 1 month; stock beats its sector ETF over
1 month; price > 200 EMA; RSI(14) 50-70; market cap > $2B.
Optional: sector ETF also beats SPY over 3 months.
Hold 30-90 days. Stop = -6%. T1 = 60-session high. T2 = analyst consensus.

Sector ETFs: XLK tech, XLF financials, XLE energy, XLV healthcare, XLI
industrials, XLY consumer discretionary, XLP staples, XLU utilities, XLRE
real estate, XLB materials, XLC communications.

Gates the program cannot check with free data (guidance changes, analyst
upgrades, institutional flows, ETF holdings, revenue surprise) are not used.
The report says so.

## 7. Confidence score

| Condition | Adjustment |
|---|---|
| Every mandatory gate passes | Base 70 |
| One mandatory gate fails | Base 45 |
| Two or more mandatory gates fail | Base = 40 x pass ratio, cap 40 = no setup |
| Each optional gate that passes | +5 |
| Volume confirms (rel. volume 1.2x+ on an up day) | +10 |
| Volume contradicts (rel. volume below 0.7x, or 1.2x+ on a down day) | -15 |
| S&P 500 below 200-DMA and long-only strategy | -20 |
| VIX above 25 | -10 |
| Fundamentals deteriorating (revenue growth below 0) | -20 |
| Earnings within 5 days and not PEAD or Earnings Run-Up | -15 |
| A disqualifier is present | Cap 40 |

Never report above 85. Never below 0.

| Score | Meaning | Action |
|---|---|---|
| 80-85 | High conviction | Full position |
| 65-79 | Valid setup | Normal position |
| 50-64 | Marginal | Half position or wait a day |
| Below 50 | No setup | Do not trade |

Runner-up: show the second strategy when it is within 15 points.

Position size for a 10,000 account at 1.5% risk:
shares = (10,000 x 0.015) / (entry - stop).

## 8. ASD-STE100 writing rules

- Use one topic in each sentence.
- Descriptive sentences: 25 words or fewer. Procedural sentences: 20 words or fewer.
- Use the active voice.
- Use simple tenses (present, past, future).
- Use approved words with one meaning each (for example "rise", "fall", "stop").
- Do not use slang, idioms or speculation words ("might skyrocket", "to the moon").
- Write numbers as digits with units.
- Give a warning before the data that it applies to.

## 9. Honesty rules

- Say "no setup" when there is none.
- Flag missing data. Missing data lowers confidence.
- Never hide a bearish read. If fundamentals are deteriorating, say it first.
- A good company and a good entry are two different questions.
- If the stock is up 15% or more in 5 sessions, say that the entry is late.
- End every report with: "Analysis, not investment advice — verify prices before acting."
