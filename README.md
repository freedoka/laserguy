# steel price scraper for `godxf.json`

## Install

Place `scraper.py`, `config.json`, `requirements.txt`, and `.github/workflows/daily-price-scraper.yml` in the root of the `freedoka/laserguy` repository, beside `godxf.json`. Commit and push the files.

Install dependencies locally with `pip install -r requirements.txt`, then run `python scraper.py`.

GitHub Actions runs daily at 05:30 UTC and can also be started manually from the Actions tab. It commits changes to `godxf.json` and `price_scraper_report.json` when prices change.

## Safety

The script stops on unexpected table structure, missing prices, invalid values, or a price change exceeding `maximum_change_percent`. Check `price_scraper_report.json` for the variants checked and the highest-priced variant selected for each thickness.
