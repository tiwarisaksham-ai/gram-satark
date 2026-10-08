# GramSatark

GramSatark is a village transparency and accountability platform built with Flask and SQLite.

## First deployment

1. Install Python 3.11+ and dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

2. Set a strong production secret and run the application:

   ```powershell
   $env:GRAMSATARK_SECRET_KEY = "replace-with-a-long-random-secret"
   $env:GRAMSATARK_HTTPS = "1"
   python app.py
   ```

3. Open `/setup` once. It creates the first administrator and the first real village. It cannot be used again after setup.

4. The administrator creates real village records, users, projects and elections. Citizens register only against those villages. Prospective representatives use `/representative/onboarding`; only verified and appointed representatives can use `/representative/login`.

## Data safety

The live application database is `gramsatark_live.db` and starts empty. The earlier `gramsatark.db` was retained locally as a non-running backup instead of being deleted.

## Production command

For a production WSGI server:

```powershell
pip install gunicorn
gunicorn app:app
```

On Windows, use a Windows-compatible WSGI server such as Waitress:

```powershell
pip install waitress
waitress-serve --listen=0.0.0.0:8000 app:app
```
