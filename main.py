"""Practice-test & homework portal for tutors.

One file. FastAPI + SQLite. Claude (via MCP) creates tests; a human edits them in
the web UI; students take them at a public link. There is NO AI inside this app:
no AI SDK, no API keys, no model calls. All the thinking happens in the user's Claude.
"""
import contextlib
import html
import json
import os
import secrets
import sqlite3
from contextvars import ContextVar
from datetime import datetime, timezone

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.responses import JSONResponse

DB_PATH = os.environ.get("DB_PATH", "portal.db")
BASE_URL_ENV = os.environ.get("BASE_URL", "").rstrip("/")
e = html.escape

# --------------------------------------------------------------------------- DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS tutors(
  id INTEGER PRIMARY KEY, name TEXT, key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS tests(
  id INTEGER PRIMARY KEY, tutor_id INTEGER REFERENCES tutors(id),
  title TEXT, subject TEXT DEFAULT '', time_limit_min INTEGER DEFAULT 0,
  slug TEXT UNIQUE, created_at TEXT);
CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY, test_id INTEGER REFERENCES tests(id) ON DELETE CASCADE,
  position INTEGER, text TEXT, options TEXT, correct_index INTEGER,
  explanation TEXT DEFAULT '', topic TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS attempts(
  id TEXT PRIMARY KEY, test_id INTEGER REFERENCES tests(id) ON DELETE CASCADE,
  student_name TEXT, score INTEGER, total INTEGER, answers TEXT, submitted_at TEXT);
"""


@contextlib.contextmanager
def conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        env_key = os.environ.get("TUTOR_KEY")
        name = os.environ.get("TUTOR_NAME", "Demo Tutor")
        if env_key:
            c.execute(
                "INSERT INTO tutors(name,key) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET name=excluded.name",
                (name, env_key),
            )
        elif not c.execute("SELECT 1 FROM tutors").fetchone():
            k = secrets.token_urlsafe(16)
            c.execute("INSERT INTO tutors(name,key) VALUES(?,?)", (name, k))
            print(f"[setup] Created tutor '{name}' with key: {k}", flush=True)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tutor_by_key(key):
    if not key:
        return None
    with conn() as c:
        return c.execute("SELECT * FROM tutors WHERE key=?", (key,)).fetchone()


# ------------------------------------------------------------ shared test logic


class QuestionIn(BaseModel):
    text: str
    options: list[str]
    correct_index: int
    explanation: str = ""
    topic: str = ""


def validate_questions(qs):
    if not qs:
        raise ValueError("A test needs at least one question.")
    for i, q in enumerate(qs, 1):
        if not q.text.strip():
            raise ValueError(f"Question {i}: text is empty.")
        if not 2 <= len(q.options) <= 6:
            raise ValueError(f"Question {i}: give between 2 and 6 options.")
        if not 0 <= q.correct_index < len(q.options):
            raise ValueError(
                f"Question {i}: correct_index must be between 0 and {len(q.options) - 1}."
            )


def insert_questions(c, test_id, qs):
    for pos, q in enumerate(qs, 1):
        c.execute(
            "INSERT INTO questions(test_id,position,text,options,correct_index,explanation,topic)"
            " VALUES(?,?,?,?,?,?,?)",
            (test_id, pos, q.text.strip(), json.dumps(q.options), q.correct_index,
             q.explanation.strip(), q.topic.strip()),
        )


def load_questions(c, test_id):
    return [
        {
            "id": r["id"], "text": r["text"], "options": json.loads(r["options"]),
            "correct_index": r["correct_index"], "explanation": r["explanation"],
            "topic": r["topic"],
        }
        for r in c.execute(
            "SELECT * FROM questions WHERE test_id=? ORDER BY position, id", (test_id,)
        )
    ]


def analyze(rows):
    """Turn attempt rows into per-attempt, per-question and per-topic stats."""
    attempts, by_q, by_topic = [], {}, {}
    for r in rows:
        answers = json.loads(r["answers"])
        attempts.append({
            "id": r["id"], "student": r["student_name"], "score": r["score"],
            "total": r["total"], "submitted_at": r["submitted_at"],
            "wrong_questions": [a["text"] for a in answers if a["chosen"] != a["correct"]],
        })
        for a in answers:
            ok = a["chosen"] == a["correct"]
            s = by_q.setdefault(a["text"], [0, 0])
            s[0] += ok
            s[1] += 1
            if a.get("topic"):
                t = by_topic.setdefault(a["topic"], [0, 0])
                t[0] += ok
                t[1] += 1
    pct = lambda s: round(100 * s[0] / s[1])
    return (
        attempts,
        [{"question": q, "percent_correct": pct(s)} for q, s in by_q.items()],
        {t: pct(s) for t, s in by_topic.items()},
    )


# ------------------------------------------------------------------- MCP server

current_key: ContextVar = ContextVar("current_key", default=None)
current_base: ContextVar = ContextVar("current_base", default=None)

mcp = FastMCP(
    "Tutor Test Portal",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def base_url():
    return BASE_URL_ENV or current_base.get() or "http://localhost:8000"


def need_tutor():
    t = tutor_by_key(current_key.get())
    if not t:
        raise ValueError("Invalid tutor key in the connector URL.")
    return t


def own_test(c, tutor, test_id):
    t = c.execute(
        "SELECT * FROM tests WHERE id=? AND tutor_id=?", (test_id, tutor["id"])
    ).fetchone()
    if not t:
        raise ValueError(f"No test with id {test_id} on this account.")
    return t


def links(t, tutor):
    b = base_url()
    return {
        "student_url": f"{b}/t/{t['slug']}",
        "edit_url": f"{b}/tests/{t['id']}/edit?key={tutor['key']}",
    }


@mcp.tool()
def create_test(
    title: str,
    questions: list[QuestionIn],
    subject: str = "",
    time_limit_minutes: int = 0,
) -> dict:
    """Create a multiple-choice practice test or homework quiz for students.

    Write accurate, well-formed questions. Each question needs: text, options (2-6
    answer choices as plain strings, without A/B/C labels), correct_index (0-based
    position of the right option), explanation (one or two sentences shown to the
    student after submitting), and topic (a short skill label such as "Kinematics"
    used to group results). time_limit_minutes of 0 means untimed.
    Returns test_id, student_url (give this to students) and edit_url (for the tutor).
    """
    tutor = need_tutor()
    validate_questions(questions)
    slug = secrets.token_urlsafe(6)
    with conn() as c:
        cur = c.execute(
            "INSERT INTO tests(tutor_id,title,subject,time_limit_min,slug,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (tutor["id"], title.strip(), subject.strip(), max(0, time_limit_minutes), slug, now()),
        )
        test_id = cur.lastrowid
        insert_questions(c, test_id, questions)
        t = c.execute("SELECT * FROM tests WHERE id=?", (test_id,)).fetchone()
    return {"test_id": test_id, "title": title, "question_count": len(questions), **links(t, tutor)}


@mcp.tool()
def list_tests() -> list[dict]:
    """List all tests on this account with question counts, attempt counts and links."""
    tutor = need_tutor()
    with conn() as c:
        rows = c.execute(
            "SELECT t.*,"
            " (SELECT COUNT(*) FROM questions WHERE test_id=t.id) AS nq,"
            " (SELECT COUNT(*) FROM attempts WHERE test_id=t.id) AS na"
            " FROM tests t WHERE tutor_id=? ORDER BY id DESC",
            (tutor["id"],),
        ).fetchall()
    return [
        {"test_id": r["id"], "title": r["title"], "subject": r["subject"],
         "question_count": r["nq"], "attempt_count": r["na"], **links(r, tutor)}
        for r in rows
    ]


@mcp.tool()
def get_test(test_id: int) -> dict:
    """Get one test in full, including correct answers and explanations, so it can be reviewed or revised."""
    tutor = need_tutor()
    with conn() as c:
        t = own_test(c, tutor, test_id)
        qs = load_questions(c, test_id)
    return {
        "test_id": t["id"], "title": t["title"], "subject": t["subject"],
        "time_limit_minutes": t["time_limit_min"],
        "questions": [{k: v for k, v in q.items() if k != "id"} for q in qs],
        **links(t, tutor),
    }


@mcp.tool()
def update_test(
    test_id: int,
    title: str | None = None,
    subject: str | None = None,
    time_limit_minutes: int | None = None,
    questions: list[QuestionIn] | None = None,
) -> dict:
    """Change an existing test. Only the fields you pass are changed.
    If you pass questions, they REPLACE the whole question list (send every question you want to keep).
    Past student attempts are kept as they were taken."""
    tutor = need_tutor()
    if questions is not None:
        validate_questions(questions)
    with conn() as c:
        t = own_test(c, tutor, test_id)
        c.execute(
            "UPDATE tests SET title=?, subject=?, time_limit_min=? WHERE id=?",
            (
                title.strip() if title is not None else t["title"],
                subject.strip() if subject is not None else t["subject"],
                max(0, time_limit_minutes) if time_limit_minutes is not None else t["time_limit_min"],
                test_id,
            ),
        )
        if questions is not None:
            c.execute("DELETE FROM questions WHERE test_id=?", (test_id,))
            insert_questions(c, test_id, questions)
        t = c.execute("SELECT * FROM tests WHERE id=?", (test_id,)).fetchone()
        n = c.execute("SELECT COUNT(*) FROM questions WHERE test_id=?", (test_id,)).fetchone()[0]
    return {"test_id": test_id, "title": t["title"], "question_count": n, **links(t, tutor)}


@mcp.tool()
def get_results(test_id: int) -> dict:
    """Get student results for a test: every attempt (name, score, which questions were
    missed), the percent correct per question, and the percent correct per topic.
    Use this to tell the tutor who is struggling and which topics to reteach."""
    tutor = need_tutor()
    with conn() as c:
        t = own_test(c, tutor, test_id)
        rows = c.execute(
            "SELECT * FROM attempts WHERE test_id=? ORDER BY submitted_at", (test_id,)
        ).fetchall()
    attempts, by_q, by_topic = analyze(rows)
    avg = round(100 * sum(a["score"] for a in attempts) / max(1, sum(a["total"] for a in attempts)))
    return {
        "test_id": test_id, "title": t["title"], "attempt_count": len(attempts),
        "average_percent": avg if attempts else None,
        "attempts": attempts, "by_question": by_q, "by_topic": by_topic,
    }


mcp_app = mcp.streamable_http_app()  # must exist before the lifespan starts its session manager


@contextlib.asynccontextmanager
async def lifespan(_app):
    init_db()
    async with mcp.session_manager.run():
        yield


api = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class McpRouter:
    """Routes /mcp/<tutor_key> to the MCP app (key stays in the URL, so Claude needs no
    OAuth or headers). Everything else goes to the website."""

    def __init__(self, site, mcp_asgi):
        self.site, self.mcp_asgi = site, mcp_asgi

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp/"):
            key = scope["path"][len("/mcp/"):].strip("/").split("/")[0]
            if not tutor_by_key(key):
                await JSONResponse({"error": "invalid key"}, status_code=401)(scope, receive, send)
                return
            h = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            proto = h.get("x-forwarded-proto", scope.get("scheme", "http"))
            host = h.get("x-forwarded-host") or h.get("host", "localhost")
            current_key.set(key)
            current_base.set(f"{proto}://{host}")
            await self.mcp_asgi(dict(scope, path="/mcp", raw_path=b"/mcp"), receive, send)
            return
        await self.site(scope, receive, send)


app = McpRouter(api, mcp_app)  # uvicorn main:app

# -------------------------------------------------------------------- web pages

CSS = """
body{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#1a1a2e;line-height:1.5}
h1{font-size:1.5rem;margin-bottom:.2rem} h2{font-size:1.15rem}
.card{border:1px solid #ddd;border-radius:10px;padding:1rem;margin:1rem 0}
input[type=text],input[type=number],textarea{width:100%;padding:.5rem;border:1px solid #bbb;border-radius:6px;box-sizing:border-box;font:inherit}
button,.btn{background:#4f46e5;color:#fff;border:0;border-radius:6px;padding:.6rem 1.1rem;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}
table{width:100%;border-collapse:collapse} td,th{border-bottom:1px solid #eee;padding:.4rem;text-align:left}
.ok{color:#15803d}.bad{color:#b91c1c}.muted{color:#666;font-size:.9rem}
.row{display:flex;gap:.5rem;align-items:center;margin:.3rem 0}
code{background:#f3f4f6;padding:.1rem .3rem;border-radius:4px;word-break:break-all}
"""


def page(title, body, status=200):
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{e(title)}</title><style>{CSS}</style></head><body>{body}</body></html>",
        status_code=status,
    )


def site_base(request: Request):
    return BASE_URL_ENV or str(request.base_url).rstrip("/")


def auth(request: Request):
    key = request.query_params.get("key") or request.cookies.get("key")
    return tutor_by_key(key), key


def keep_cookie(resp, request, key):
    if request.query_params.get("key") and key:  # one-click link from Claude -> stay logged in
        resp.set_cookie("key", key, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


def login_page(msg=""):
    return page("Sign in", f"""
<h1>Tutor sign in</h1><p class="muted">Paste your tutor key.</p>
{f'<p class="bad">{e(msg)}</p>' if msg else ''}
<form method="post" action="/login" class="card">
<input type="text" name="key" placeholder="Tutor key" required><p></p>
<button>Sign in</button></form>""")


@api.get("/health")
def health():
    return PlainTextResponse("ok")


@api.post("/login")
def login(key: str = Form("")):
    if not tutor_by_key(key.strip()):
        return login_page("That key is not valid.")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("key", key.strip(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@api.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("key")
    return resp


# Page 1: tutor dashboard
@api.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    tutor, key = auth(request)
    if not tutor:
        return login_page()
    with conn() as c:
        rows = c.execute(
            "SELECT t.*,"
            " (SELECT COUNT(*) FROM questions WHERE test_id=t.id) AS nq,"
            " (SELECT COUNT(*) FROM attempts WHERE test_id=t.id) AS na"
            " FROM tests t WHERE tutor_id=? ORDER BY id DESC",
            (tutor["id"],),
        ).fetchall()
    b = site_base(request)
    trs = "".join(
        f"<tr><td><b>{e(r['title'])}</b><br><span class='muted'>{e(r['subject'])}</span></td>"
        f"<td>{r['nq']}</td><td>{r['na']}</td>"
        f"<td><a href='/tests/{r['id']}/edit'>Edit &amp; results</a><br>"
        f"<a href='/t/{e(r['slug'])}' target='_blank'>Student link</a></td></tr>"
        for r in rows
    ) or "<tr><td colspan='4' class='muted'>No tests yet. Ask Claude to create one.</td></tr>"
    resp = page("Tests", f"""
<h1>{e(tutor['name'])}'s tests</h1>
<p class="muted"><a href="/logout">Sign out</a></p>
<div class="card"><table><tr><th>Test</th><th>Questions</th><th>Attempts</th><th></th></tr>{trs}</table></div>
<div class="card"><b>Connect Claude</b>
<p class="muted">Claude &rarr; Customize &rarr; Connectors &rarr; Add custom connector, then paste this URL:</p>
<code>{e(b)}/mcp/{e(key)}</code></div>""")
    return keep_cookie(resp, request, key)


# Page 2: editor + results
def render_editor(request, t, qs, attempts_rows, saved=False):
    b = site_base(request)
    blocks = ""
    for n, q in enumerate(qs, 1):
        opts = "".join(
            f"<div class='row'><input type='radio' name='q{q['id']}_correct' value='{j}'"
            f"{' checked' if j == q['correct_index'] else ''}>"
            f"<input type='text' name='q{q['id']}_opt{j}' value='{e(o)}'></div>"
            for j, o in enumerate(q["options"])
        )
        blocks += f"""<div class="card"><b>Q{n}</b>
<label class="muted" style="float:right"><input type="checkbox" name="del_{q['id']}"> delete</label>
<textarea name="q{q['id']}_text" rows="2">{e(q['text'])}</textarea>
<p class="muted">Pick the radio button next to the correct answer.</p>{opts}
<div class="row"><input type="text" name="q{q['id']}_topic" placeholder="Topic" value="{e(q['topic'])}"></div>
<div class="row"><input type="text" name="q{q['id']}_exp" placeholder="Explanation" value="{e(q['explanation'])}"></div></div>"""
    new_opts = "".join(
        f"<div class='row'><input type='radio' name='new_correct' value='{j}'{' checked' if j == 0 else ''}>"
        f"<input type='text' name='new_opt{j}' placeholder='Option {j + 1}'></div>"
        for j in range(4)
    )
    attempts, by_q, by_topic = analyze(attempts_rows)
    arows = "".join(
        f"<tr><td>{e(a['student'])}</td><td>{a['score']}/{a['total']}</td>"
        f"<td class='muted'>{e(a['submitted_at'])}</td>"
        f"<td><a href='/t/{e(t['slug'])}/result/{e(a['id'])}'>view</a></td></tr>"
        for a in attempts
    ) or "<tr><td colspan='4' class='muted'>No attempts yet.</td></tr>"
    qrows = "".join(
        f"<tr><td>{e(x['question'])}</td><td>{x['percent_correct']}%</td></tr>" for x in by_q
    )
    trows = "".join(f"<tr><td>{e(k)}</td><td>{v}%</td></tr>" for k, v in by_topic.items())
    return page(f"Edit {t['title']}", f"""
<p><a href="/">&larr; All tests</a></p>
<h1>Edit test</h1>
{'<p class="ok">Saved.</p>' if saved else ''}
<div class="card"><b>Student link</b><br><code>{e(b)}/t/{e(t['slug'])}</code></div>
<form method="post" action="/tests/{t['id']}/edit">
<div class="card">
<label>Title<input type="text" name="title" value="{e(t['title'])}" required></label><p></p>
<label>Subject<input type="text" name="subject" value="{e(t['subject'])}"></label><p></p>
<label>Time limit in minutes (0 = untimed)<input type="number" name="time_limit" min="0" value="{t['time_limit_min']}"></label>
</div>
{blocks}
<div class="card"><b>Add a question</b> <span class="muted">(leave blank to skip)</span>
<textarea name="new_text" rows="2" placeholder="Question text"></textarea>{new_opts}
<div class="row"><input type="text" name="new_topic" placeholder="Topic"></div>
<div class="row"><input type="text" name="new_exp" placeholder="Explanation"></div></div>
<button>Save changes</button></form>
<h2>Results</h2>
<div class="card"><table><tr><th>Student</th><th>Score</th><th>Submitted (UTC)</th><th></th></tr>{arows}</table></div>
{f'<div class="card"><b>By topic</b><table>{trows}</table></div>' if trows else ''}
{f'<div class="card"><b>By question</b><table>{qrows}</table></div>' if qrows else ''}""")


def editable_test(request, test_id):
    tutor, key = auth(request)
    if not tutor:
        return None, None, None
    with conn() as c:
        t = c.execute("SELECT * FROM tests WHERE id=? AND tutor_id=?", (test_id, tutor["id"])).fetchone()
    return tutor, key, t


@api.get("/tests/{test_id}/edit", response_class=HTMLResponse)
def edit_get(request: Request, test_id: int):
    tutor, key, t = editable_test(request, test_id)
    if not tutor:
        return login_page()
    if not t:
        return page("Not found", "<h1>Test not found</h1><p><a href='/'>Back</a></p>", 404)
    with conn() as c:
        qs = load_questions(c, test_id)
        rows = c.execute("SELECT * FROM attempts WHERE test_id=? ORDER BY submitted_at", (test_id,)).fetchall()
    return keep_cookie(render_editor(request, t, qs, rows), request, key)


@api.post("/tests/{test_id}/edit", response_class=HTMLResponse)
async def edit_post(request: Request, test_id: int):
    tutor, key, t = editable_test(request, test_id)
    if not tutor:
        return login_page()
    if not t:
        return page("Not found", "<h1>Test not found</h1>", 404)
    f = await request.form()
    g = lambda name, d="": (f.get(name) or d).strip()
    with conn() as c:
        try:
            tl = max(0, int(g("time_limit", "0")))
        except ValueError:
            tl = 0
        c.execute("UPDATE tests SET title=?, subject=?, time_limit_min=? WHERE id=?",
                  (g("title", t["title"]), g("subject"), tl, test_id))
        for q in load_questions(c, test_id):
            qid = q["id"]
            if f.get(f"del_{qid}"):
                c.execute("DELETE FROM questions WHERE id=?", (qid,))
                continue
            opts = [g(f"q{qid}_opt{j}") or "(blank)" for j in range(len(q["options"]))]
            try:
                ci = int(g(f"q{qid}_correct", str(q["correct_index"])))
            except ValueError:
                ci = q["correct_index"]
            ci = ci if 0 <= ci < len(opts) else q["correct_index"]
            c.execute(
                "UPDATE questions SET text=?, options=?, correct_index=?, explanation=?, topic=? WHERE id=?",
                (g(f"q{qid}_text", q["text"]), json.dumps(opts), ci, g(f"q{qid}_exp"), g(f"q{qid}_topic"), qid),
            )
        if g("new_text"):
            opts = [g(f"new_opt{j}") or "(blank)" for j in range(4)]
            try:
                ci = int(g("new_correct", "0"))
            except ValueError:
                ci = 0
            insert_questions(c, test_id, [QuestionIn(
                text=g("new_text"), options=opts, correct_index=ci if 0 <= ci < 4 else 0,
                explanation=g("new_exp"), topic=g("new_topic"))])
        for pos, r in enumerate(c.execute(
                "SELECT id FROM questions WHERE test_id=? ORDER BY position, id", (test_id,)).fetchall(), 1):
            c.execute("UPDATE questions SET position=? WHERE id=?", (pos, r["id"]))
        t = c.execute("SELECT * FROM tests WHERE id=?", (test_id,)).fetchone()
        qs = load_questions(c, test_id)
        rows = c.execute("SELECT * FROM attempts WHERE test_id=? ORDER BY submitted_at", (test_id,)).fetchall()
    return render_editor(request, t, qs, rows, saved=True)


# Page 3: student takes the test
TIMER_JS = """<script>(function(){var m=__LIMIT__;if(!m)return;var end=Date.now()+m*60000,
el=document.getElementById('timer'),f=document.getElementById('f');
setInterval(function(){var s=Math.max(0,Math.round((end-Date.now())/1000));
el.textContent=Math.floor(s/60)+':'+String(s%60).padStart(2,'0');if(s===0)f.submit();},500);})();</script>"""


def test_by_slug(slug):
    with conn() as c:
        t = c.execute("SELECT * FROM tests WHERE slug=?", (slug,)).fetchone()
        qs = load_questions(c, t["id"]) if t else []
    return t, qs


@api.get("/t/{slug}", response_class=HTMLResponse)
def take_test(slug: str):
    t, qs = test_by_slug(slug)
    if not t:
        return page("Not found", "<h1>Test not found</h1>", 404)
    blocks = ""
    for n, q in enumerate(qs, 1):
        opts = "".join(
            f"<label class='row'><input type='radio' name='q{q['id']}' value='{j}'> {e(o)}</label>"
            for j, o in enumerate(q["options"])
        )
        blocks += f"<div class='card'><b>{n}. {e(q['text'])}</b>{opts}</div>"
    limit = t["time_limit_min"]
    timer = f"<p><b>Time left: <span id='timer'>{limit}:00</span></b></p>" if limit else ""
    return page(t["title"], f"""
<h1>{e(t['title'])}</h1><p class="muted">{e(t['subject'])}{' · ' if t['subject'] and limit else ''}{f'{limit} minutes' if limit else ''}</p>
{timer}
<form id="f" method="post" action="/t/{e(slug)}">
<div class="card"><label>Your name<input type="text" name="student_name" required maxlength="80"></label></div>
{blocks}
<button>Submit</button></form>
{TIMER_JS.replace('__LIMIT__', str(int(limit))) if limit else ''}""")


@api.post("/t/{slug}")
async def submit_test(request: Request, slug: str):
    t, qs = test_by_slug(slug)
    if not t:
        return page("Not found", "<h1>Test not found</h1>", 404)
    f = await request.form()
    name = (f.get("student_name") or "").strip()[:80] or "Anonymous"
    snapshot, score = [], 0
    for q in qs:
        raw = f.get(f"q{q['id']}")
        chosen = int(raw) if raw is not None and raw.isdigit() and int(raw) < len(q["options"]) else None
        score += chosen == q["correct_index"]
        snapshot.append({"text": q["text"], "options": q["options"], "chosen": chosen,
                         "correct": q["correct_index"], "explanation": q["explanation"],
                         "topic": q["topic"]})
    aid = secrets.token_urlsafe(8)
    with conn() as c:
        c.execute(
            "INSERT INTO attempts(id,test_id,student_name,score,total,answers,submitted_at) VALUES(?,?,?,?,?,?,?)",
            (aid, t["id"], name, score, len(qs), json.dumps(snapshot), now()),
        )
    return RedirectResponse(f"/t/{slug}/result/{aid}", status_code=303)


# Page 4: student result
@api.get("/t/{slug}/result/{attempt_id}", response_class=HTMLResponse)
def result(slug: str, attempt_id: str):
    with conn() as c:
        r = c.execute(
            "SELECT a.*, t.title FROM attempts a JOIN tests t ON t.id=a.test_id WHERE a.id=? AND t.slug=?",
            (attempt_id, slug),
        ).fetchone()
    if not r:
        return page("Not found", "<h1>Result not found</h1>", 404)
    items = ""
    for n, a in enumerate(json.loads(r["answers"]), 1):
        ok = a["chosen"] == a["correct"]
        yours = e(a["options"][a["chosen"]]) if a["chosen"] is not None else "<i>no answer</i>"
        items += f"""<div class="card"><b>{n}. {e(a['text'])}</b>
<p class="{'ok' if ok else 'bad'}">{'Correct' if ok else 'Incorrect'} · your answer: {yours}</p>
{'' if ok else f"<p>Right answer: <b>{e(a['options'][a['correct']])}</b></p>"}
{f"<p class='muted'>{e(a['explanation'])}</p>" if a['explanation'] else ''}</div>"""
    pct = round(100 * r["score"] / max(1, r["total"]))
    return page("Result", f"""
<h1>{e(r['title'])}</h1>
<div class="card"><h2>{e(r['student_name'])}: {r['score']} / {r['total']} ({pct}%)</h2></div>{items}""")
