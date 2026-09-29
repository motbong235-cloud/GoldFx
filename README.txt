Gold Fx update — copy these files over your project, then redeploy on Render:

  templates/dashboard.html   -> templates/dashboard.html
  server.py                  -> server.py
  signal_engine.py           -> signal_engine.py

Changes
- Removed Levels tab and Code tab (Pro included); /api/indicator now returns empty
- Removed indicator lines from the TradingView chart
- Market proxy no longer blocks workers (max ~6s, 5s fail cooldown)
- UI/UX: segmented timeframe bar, framed chart, calmer notice, larger touch targets,
  16px inputs (no iOS zoom), pinch-zoom allowed, loading skeletons, live price
  turns red/green by direction, Clear all + auto-read for alerts, Esc closes panels
