# Macro Outlook Dashboard

A live dashboard of U.S. monetary policy, rates, inflation, labor/growth and fiscal data,
pulled from the Federal Reserve's FRED database, with a monthly written market outlook.
Data refreshes on its own: it's cached for one hour, so new releases appear automatically.

## Run it on your computer

1. Get a free API key: https://fred.stlouisfed.org/docs/api/api_key.html
2. In this folder, create `.streamlit/secrets.toml` containing:
   ```
   FRED_API_KEY = "paste_your_key_here"
   ```
3. Install and run:
   ```
   pip install -r requirements.txt
   streamlit run app.py
   ```
   It opens in your browser at http://localhost:8501

## Put it online (free)

1. Create a new GitHub repo and upload everything in this folder **except** `secrets.toml`
   (the `.gitignore` already keeps it out).
2. Go to https://share.streamlit.io, sign in with GitHub, click **Create app**, pick the repo, main file `app.py`.
3. Under **Advanced settings → Secrets**, paste the same `FRED_API_KEY = "..."` line.
4. Deploy. You get a public link for your resume and LinkedIn.

## Monthly routine (10 minutes)

Edit `outlook.md` on GitHub with your view and commit it. The live site updates automatically.

## Adding an indicator

Add one line to the `SERIES` dictionary in `app.py` with its FRED code, then call
`make_chart(...)` in the tab where it belongs.
