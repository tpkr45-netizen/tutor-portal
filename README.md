# Tutor Test Portal

A practice-test and homework portal for tutors. **There is no AI inside this app.**
The tutor's own Claude writes the tests and sends them in over MCP. The app stores them,
lets a person edit them, serves them to students at a public link, grades them, and keeps
the results. Claude can then read the results back and tell the tutor what to reteach.

Everything is in `main.py` (FastAPI + SQLite). Dependencies: `fastapi`, `uvicorn`, `mcp` (pinned below 2.x), `python-multipart`.

## What it does

| Piece | Details |
|---|---|
| MCP tools (5) | `create_test`, `list_tests`, `get_test`, `update_test`, `get_results` |
| Web pages (4) | `/` tutor dashboard, `/tests/{id}/edit` editor + results, `/t/{slug}` student test, `/t/{slug}/result/{id}` student result |
| Auth | One tutor key. It is the tutor's website login and also sits in the MCP URL: `https://YOUR-DOMAIN/mcp/<TUTOR_KEY>` |
| Database | SQLite file, 4 tables: `tutors`, `tests`, `questions`, `attempts` |

Attempts store a snapshot of each question as the student saw it, so editing a test later never corrupts old results.

## 1. Run it locally (2 minutes)

```bash
pip install -r requirements.txt
TUTOR_KEY=mysecretkey uvicorn main:app --port 8000
```

Open http://localhost:8000, paste `mysecretkey`. The MCP endpoint is `http://localhost:8000/mcp/mysecretkey`.
Claude cannot reach localhost, so you need step 2 before connecting it.

## 2. Deploy on Railway (about 20-30 minutes)

SQLite needs a disk that survives restarts, so use a host with a volume. Do not use Vercel or Netlify.
Button names may differ slightly from these labels.

1. Put `main.py`, `requirements.txt` and `Procfile` in a new GitHub repo (or use the Railway CLI with `railway up`).
2. Railway: **New Project -> Deploy from GitHub repo** and pick it. It reads `requirements.txt`, and the `Procfile` supplies the start command.
3. In the service, add a **Volume** and mount it at `/data`.
4. In **Variables**, add:
   - `DB_PATH` = `/data/portal.db`
   - `TUTOR_KEY` = a long random string. Generate one: `python -c "import secrets; print(secrets.token_urlsafe(24))"`
   - `TUTOR_NAME` = your name (shown on the dashboard)
5. **Settings -> Networking -> Generate Domain**. You get `https://something.up.railway.app`.
6. Check `https://YOUR-DOMAIN/health` returns `ok`, then open `/` and sign in with your key.

Make sure the service is awake and not on a plan that sleeps when Volopay opens it. Open the link yourself right before you submit.

## 3. Connect it to Claude

1. In Claude: **Customize -> Connectors -> Add custom connector**.
2. Name: `Tutor Test Portal`. URL: `https://YOUR-DOMAIN/mcp/<TUTOR_KEY>`.
3. Leave authentication at no sign-in. Do not add OAuth or headers. The key in the URL is the credential.
4. Add it, and make sure it is switched on in your chat.

The Free plan allows one custom connector. On a Team or Enterprise plan an owner adds it under Organization settings -> Connectors.

**Smoke test, do this first:** ask Claude "List my tests". An empty list means the connection works.

## 4. Demo script (2 minutes)

1. In Claude, type: *"Create a 10-question Class 11 physics test on kinematics, medium difficulty, 15-minute limit. Include an explanation and a topic for every question."*
2. Open the `edit_url` Claude returns. The test is there. Change one question by hand (fix wording, switch the correct answer) and click **Save changes**.
3. Open the student link in another tab or on your phone. Enter a name, answer, submit, show the score and explanations.
4. Back in Claude: *"How did my students do on that test? Which topics should I reteach?"* Claude calls `get_results` and does the analysis.

Do one full dry run first. Take 2-3 attempts with different names before recording so step 4 has real data.

## 5. Pitch outline (5 minutes)

| Segment | Time | What to cover |
|---|---|---|
| Who you are | 45s | Product manager, about 3 years across fintech, ERP and AI products. You also tutor students, so you built this for a problem you have. |
| Your best work | 30s | One thing you did well, with a real number or proof on screen. Pick your strongest. |
| Why it wins | 1m | Who it's for: independent tutors and small coaching centres. Problem: Claude can write a test, but it can't give students a link, record attempts, grade, or remember results. The app is the part that stays live. |
| Demo | 2m | Section 4 above. |
| The money | 45s | See below. |

**The money, suggested answers (adjust to your own judgment):**
- **Cost per customer:** hosting and a tiny database are the only costs, shared across all tutors. Check Railway's current pricing and quote the real monthly figure divided by tutors. No AI bill, because the tutor's own Claude plan does the thinking.
- **Price:** a free tier (for example 2 tests) and a paid plan per tutor per month. Pick a number you can defend against what a tutor earns per student.
- **First 100 customers:** you are a tutor yourself. Start with tutor groups, WhatsApp/Telegram communities, Superprof-style marketplaces and coaching-centre owners you already know. Offer free setup to the first 20 and ask each for two referrals.
- **Year 1 / Year 3:** year 1, a reliable tool for solo tutors (homework, mock tests, parent-ready reports). Year 3, multi-tutor coaching centres, student accounts, parent reports and a template library, still with no AI cost inside the app.

## 6. What to send

Email **sandeep.juttuga@volopay.co** and **rosh@volopay.co**:

1. **Video:** link, 5 minutes or less.
2. **Live app:** `https://YOUR-DOMAIN`. Login: paste the tutor key on the sign-in page. Or send the one-click link `https://YOUR-DOMAIN/?key=<TUTOR_KEY>`.
3. **MCP link:** `https://YOUR-DOMAIN/mcp/<TUTOR_KEY>`, plus the connect steps from section 3.

Add one line: *"There are no AI keys or model calls anywhere in the code; Claude does all the thinking through MCP."*

## 7. Pre-submit checklist

- [ ] `/health` returns `ok` on the live URL
- [ ] Fresh Claude chat: "List my tests" works through the connector
- [ ] Create from Claude, edit in the app, take the test as a student, ask Claude for results
- [ ] Test the live student link on your phone
- [ ] Video is 5:00 or under, and the link opens when logged out
- [ ] Email includes video, app link + login, MCP link + steps
- [ ] Search the repo for `openai`, `anthropic`, `api_key` and find no SDKs or keys (comments saying "Claude" are fine)

## Known limits (be upfront if asked)

- One shared tutor key, no passwords or multi-tutor signup yet. Extra tutors can be added by inserting rows in `tutors`.
- The key is in the MCP URL and in the one-click edit link, so treat those as secrets.
- Students enter a name only. There is no student login.
- SQLite on one instance is right for a first version, not for heavy scale.
- `mcp` is pinned below 2.x because 2.x renamed the server class. Don't loosen the pin without migrating.
