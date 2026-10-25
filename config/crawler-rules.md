# Crawler rules

The crawler in `src/ticker_review/crawler.py` applies these rules.

- Use SAFE_SEARCH=true when crawling.
- Sleep for CRAWLER_SLEEP_MS between each request.
- Stop crawling after MAX_NEWS_ITEMS.
- If a page blocks access, retry once after sleep.
- If still blocked, skip and log.
- Use short user-agent string.
- Avoid aggressive crawling.
- Respect robots.txt.

## How the program applies the rules

| Rule | Implementation |
|---|---|
| Safe search | `SAFE_SEARCH=true` sends `safesearch=on` to the search provider. `false` sends `moderate`. |
| Sleep | Each request (search, robots.txt, page) starts at least `CRAWLER_SLEEP_MS` after the previous one. A larger `Crawl-delay` in robots.txt wins (max 10 s). |
| Item limit | `MAX_NEWS_ITEMS` counts pages that were read. It applies to each crawl: one crawl per ticker, plus one shared macro crawl per market (US, India) per run. |
| Blocked page | HTTP 401, 403, 429, 451, 503 or a network error. The crawler sleeps, then tries once more. A second failure logs the URL as `blocked` and skips it. Blocked pages do not count toward the limit. |
| Timeout | `CRAWLER_TIMEOUT_MS` applies to each HTTP request. |
| User agent | `stock-review-bot/1.0`. |
| robots.txt | Read once per host. 401/403 = disallow all. Other 4xx = allow all. 5xx or network error = disallow all (safe default). A disallowed URL is logged as `robots_disallowed`. |
| Page size | The crawler reads at most 1.5 MB of each page. |

## Trust labels

| Label | Domains |
|---|---|
| trusted | reuters.com, bloomberg.com, wsj.com, cnbc.com, ft.com, apnews.com, barrons.com, marketwatch.com, nasdaq.com, sec.gov, nytimes.com, economist.com, federalreserve.gov, rbi.org.in, nseindia.com, bseindia.com, economictimes.indiatimes.com, livemint.com, business-standard.com, moneycontrol.com |
| semi-trusted | finance.yahoo.com, yahoo.com, seekingalpha.com, investing.com, fool.com, benzinga.com, marketbeat.com, zacks.com, thestreet.com, businessinsider.com, forbes.com, investopedia.com, morningstar.com, tipranks.com, cnn.com, bbc.com, theguardian.com, axios.com, techcrunch.com, theverge.com |
| unknown | All other domains |

The analyzer uses unknown sources only as weak evidence (rating 1).
