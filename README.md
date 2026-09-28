# Gold Fx

Flask web app — chart · Pro (Khmer System) · Admin

## Pages
| URL | File |
|-----|------|
| `/` | `templates/index.html` — chart, login, Pro |
| `/admin` | `templates/admin.html` — orders, users, KS settings |

## Entry
- **server.py** — main (gunicorn `server:app`)
- `app.py` — compatibility import

## Run
```bash
pip install -r requirements.txt
python server.py
# http://127.0.0.1:5000
# http://127.0.0.1:5000/admin  (password: admin123)
```

## Render
1. Web Service · Python
2. Start: `gunicorn server:app --bind 0.0.0.0:$PORT`
3. Disk mount `/var/data` · Env `DATA_DIR=/var/data`
4. Admin → Settings → Khmer System Profile Key

## Structure
```
gold-fx-app/
  server.py
  khmer_system.py
  requirements.txt
  Procfile
  render.yaml
  data/db.json
  static/logo-icon.jpg
  static/logo-wordmark.jpg
  templates/index.html
  templates/admin.html
```
