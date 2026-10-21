# Price data (not included)

Put **one CSV file per futures contract** in this folder, named by its symbol: root + month code + 2-digit year.

```
CTH24.csv   Cotton March 2024
CTK24.csv   Cotton May 2024
CTN24.csv   Cotton July 2024
CTZ24.csv   Cotton December 2024
```

Month codes: F Jan, G Feb, H Mar, J Apr, K May, M Jun, N Jul, Q Aug, U Sep, V Oct, X Nov, Z Dec.

## Expected columns (Barchart "historical data" download)

```
Time,Open,High,Low,Latest,Change,%Change,Volume,"Open Int"
2024-06-03,72.10,72.85,71.60,72.48,0.31,+0.43%,23101,98745
...
"Downloaded from Barchart.com as of 10-14-2026"      <- footer line, skipped automatically
```

* `Time`: trading date, `yyyy-mm-dd`
* `Latest`: daily **settlement** price
* `Volume` and `Open Int`: for **that contract only** (not totals across all contracts)

Other sources work too: rename the columns to these names, or adapt `read_barchart_csv()` in `continuous_futures.py`.

**Tips from building this series:**
* Download each contract's **full life** (from first listing to expiry), daily.
* Don't open and re-save the files in Excel. It can silently rewrite dates in your local format.
* For cotton 2010–2026 you need Mar, May, Jul and Dec contracts: about 70 files.

Files in this folder are ignored by git (see `.gitignore`), so price data can't be published by accident.
