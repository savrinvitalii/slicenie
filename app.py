# -*- coding: utf-8 -*-
"""«Сличение» — сервис афоризмов, сравнения и собственных мыслей."""
import datetime as dt
import hashlib
import os
import secrets
import sqlite3
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from markupsafe import Markup, escape
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SLICHENIE_DB", os.path.join(BASE, "slichenie.db"))
# На бесплатном хостинге (Vercel) файловая система только для чтения, поэтому там сайт работает
# в режиме «только просмотр»: база берётся готовой, вход и записи отключены.
READ_ONLY = bool(os.environ.get("VERCEL") or os.environ.get("SLICHENIE_READONLY"))
MAX_COMPARE = 15
PAGE_SIZE = 20

app = Flask(__name__)


def _secret():
    path = os.path.join(BASE, ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.secret_key = os.environ.get("SLICHENIE_SECRET") or ("slichenie-readonly-demo" if READ_ONLY else _secret())
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


# ───────────────────────── база данных ─────────────────────────
def db():
    if "db" not in g:
        if READ_ONLY:
            g.db = sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True)
        else:
            g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        # встроенный LIKE не различает регистр только у латиницы; для кириллицы нужна своя функция
        g.db.create_function("ci", 1, lambda v: v.casefold() if isinstance(v, str) else v)
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    with open(os.path.join(BASE, "schema.sql"), encoding="utf-8") as f:
        con = sqlite3.connect(DB_PATH)
        con.executescript(f.read())
        cols = [r[1] for r in con.execute("PRAGMA table_info(authors)")]
        if "achievements" not in cols:   # база создана старой версией
            con.execute("ALTER TABLE authors ADD COLUMN achievements TEXT NOT NULL DEFAULT ''")
        tcols = [r[1] for r in con.execute("PRAGMA table_info(themes)")]
        if "section_id" not in tcols:
            con.execute("ALTER TABLE themes ADD COLUMN section_id INTEGER REFERENCES sections(id) ON DELETE SET NULL")
        if "sort" not in tcols:
            con.execute("ALTER TABLE themes ADD COLUMN sort INTEGER NOT NULL DEFAULT 0")
        con.commit()
        con.close()


def apply_taxonomy():
    """Двухуровневые темы: разделы (для навигации) и темы-подтемы (их назначают афоризмам).
    Схема — data/taxonomy.json, разметка афоризмов — data/aphorism_themes.json (ключ — хеш текста афоризма).
    Применяется один раз на каждую версию схемы; дальнейшие правки тем людьми не затираются."""
    import hashlib, json, random
    p_tax = os.path.join(BASE, "data", "taxonomy.json")
    p_map = os.path.join(BASE, "data", "aphorism_themes.json")
    if not (os.path.exists(p_tax) and os.path.exists(p_map) and os.path.exists(DB_PATH)):
        return
    with open(p_tax, encoding="utf-8") as f:
        tax = json.load(f)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    row = con.execute("SELECT v FROM meta WHERE k='taxonomy'").fetchone()
    if row and int(row[0]) >= tax["version"]:
        con.close()
        return
    with open(p_map, encoding="utf-8") as f:
        amap = json.load(f)
    keep = []
    for si, sec in enumerate(tax["sections"]):
        con.execute("INSERT INTO sections(slug, name, sort) VALUES (?,?,?) "
                    "ON CONFLICT(slug) DO UPDATE SET name=excluded.name, sort=excluded.sort", (sec["slug"], sec["name"], si))
        sid = con.execute("SELECT id FROM sections WHERE slug=?", (sec["slug"],)).fetchone()[0]
        for ti, (slug, name, _hint) in enumerate(sec["themes"]):
            con.execute("INSERT INTO themes(slug, name, section_id, sort) VALUES (?,?,?,?) "
                        "ON CONFLICT(slug) DO UPDATE SET name=excluded.name, section_id=excluded.section_id, sort=excluded.sort",
                        (slug, name, sid, ti))
            keep.append(slug)
    con.execute("DELETE FROM themes WHERE slug NOT IN (%s)" % ",".join("?" * len(keep)), keep)  # старые темы
    tid = {r[0]: r[1] for r in con.execute("SELECT slug, id FROM themes")}
    con.execute("DELETE FROM aphorism_themes")
    for aid, text in con.execute("SELECT id, text FROM aphorisms").fetchall():
        h = hashlib.sha1(text.strip().encode()).hexdigest()[:12]
        for slug in amap.get(h, []):
            con.execute("INSERT OR IGNORE INTO aphorism_themes VALUES (?,?)", (aid, tid[slug]))
    # «пульс» демонстрационных людей строим заново по новым темам
    rnd = random.Random(7)
    weights = {slug: rnd.uniform(0.3, 1.6) for slug in tid}
    now = dt.datetime.utcnow()
    demo = [r[0] for r in con.execute("SELECT id FROM users WHERE is_demo=1")]
    if demo:
        con.execute("DELETE FROM events WHERE user_id IN (%s)" % ",".join("?" * len(demo)), demo)
        inv = {v: k for k, v in tid.items()}
        for uid in demo:
            for (aid,) in con.execute("SELECT aphorism_id FROM reactions WHERE user_id=?", (uid,)).fetchall():
                for (t,) in con.execute("SELECT theme_id FROM aphorism_themes WHERE aphorism_id=?", (aid,)).fetchall():
                    if rnd.random() < weights[inv[t]] / 1.6:
                        ts = now - dt.timedelta(days=rnd.uniform(0, 14), hours=rnd.uniform(0, 12))
                        con.execute("INSERT INTO events(user_id, theme_id, created_at) VALUES (?,?,?)",
                                    (uid, t, ts.strftime("%Y-%m-%d %H:%M:%S")))
    con.execute("INSERT INTO meta(k, v) VALUES ('taxonomy', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(tax["version"]),))
    con.commit()
    con.close()


def apply_authors_info():
    """Биографии (20–30 слов) и 10 главных достижений из data/authors_info.json.
    Приводит тексты в базе к файлу; безопасно вызывать при каждом запуске."""
    path = os.path.join(BASE, "data", "authors_info.json")
    if not os.path.exists(path) or not os.path.exists(DB_PATH):
        return
    import json
    with open(path, encoding="utf-8") as f:
        info = json.load(f)
    con = sqlite3.connect(DB_PATH)
    for name, v in info.items():
        con.execute("UPDATE authors SET bio=?, achievements=? WHERE name=? AND (bio<>? OR achievements<>?)",
                    (v["bio"], "\n".join(v["achievements"]), name, v["bio"], "\n".join(v["achievements"])))
    con.commit()
    con.close()


def q(sql, *a):
    return db().execute(sql, a).fetchall()


def q1(sql, *a):
    return db().execute(sql, a).fetchone()


def ex(sql, *a):
    cur = db().execute(sql, a)
    db().commit()
    return cur


# ───────────────────────── пользователи, CSRF ─────────────────────────
@app.before_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid and not READ_ONLY:
        g.user = q1("SELECT * FROM users WHERE id=?", uid)
    if request.method == "POST":
        tok = session.get("csrf")
        if not tok or tok != request.form.get("csrf"):
            abort(400, "Сессия устарела — обновите страницу и повторите.")


READ_ONLY_ENDPOINTS = {"static", "home", "themes", "theme", "section", "aphorisms", "eras_page", "authors", "author",
                       "author_about", "aphorism", "compare", "compare_add", "compare_remove", "compare_clear",
                       "thought", "profile"}


@app.before_request
def read_only_guard():
    if READ_ONLY and request.endpoint is not None and request.endpoint not in READ_ONLY_ENDPOINTS:
        return render_template("readonly.html"), 403


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not g.user:
            flash("Войдите, чтобы продолжить.")
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return w


def kin_pair(a, b):
    return (a, b) if a < b else (b, a)


def is_kin(a, b):
    if not a or not b or a == b:
        return False
    x, y = kin_pair(a, b)
    return q1("SELECT 1 FROM kin WHERE user_a=? AND user_b=? AND status='confirmed'", x, y) is not None


def kin_ids(uid):
    rows = q("""SELECT CASE WHEN user_a=? THEN user_b ELSE user_a END AS other
                FROM kin WHERE (user_a=? OR user_b=?) AND status='confirmed'""", uid, uid, uid)
    return [r["other"] for r in rows]


def log_event(aphorism_ids):
    """Запомнить, что человек размышлял о темах этих афоризмов."""
    if not g.user:
        return
    for aid in aphorism_ids:
        for t in q("SELECT theme_id FROM aphorism_themes WHERE aphorism_id=?", aid):
            db().execute("INSERT INTO events(user_id, theme_id) VALUES (?,?)", (g.user["id"], t["theme_id"]))
    db().commit()


# ───────────────────────── фильтры и контекст ─────────────────────────
def fmt_year(year, note=""):
    if year is None:
        return ""
    s = f"{-year} до н.э." if year < 0 else str(year)
    if note:
        if note.startswith("ок.") or note.startswith("по традиции") or note.startswith("приписывается"):
            s = (note.split(",")[0] + " " + s) if note.startswith("ок.") else s + f" ({note})"
        else:
            s += f" ({note})"
    return s


def fmt_dt(s):
    if not s:
        return ""
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return s
    months = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
    return f"{d.day} {months[d.month-1]} {d.year}"


@app.template_filter("year")
def _year(row):
    return fmt_year(row["year"], row["year_note"])


@app.template_filter("date")
def _date(s):
    return fmt_dt(s)


@app.template_filter("people")
def _people(n):
    n = int(n)
    word = "человек"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        word = "человека"
    return f"{n} {word}"


@app.template_filter("paras")
def _paras(s):
    parts = [p.strip() for p in (s or "").split("\n") if p.strip()]
    return Markup("".join(f"<p>{escape(p)}</p>" for p in parts))


@app.context_processor
def inject():
    return dict(csrf=csrf_token, me=g.get("user"), readonly=READ_ONLY,
                basket=session.get("compare", []),
                nav_themes=q("SELECT slug, name FROM themes ORDER BY name"))


# ───────────────────────── портрет одной линией ─────────────────────────
def portrait_svg(seed, size=160):
    """Детерминированный профиль, нарисованный одной непрерывной линией."""
    h = hashlib.sha256(str(seed).encode()).digest()
    r = lambda i, lo, hi: lo + (h[i] % 256) / 255 * (hi - lo)
    nose = r(0, 10, 26)
    brow = r(1, 0, 6)
    chin = r(2, 186, 202)
    chin_x = r(3, 112, 134)
    crown = r(4, 16, 34)
    fore = r(5, 140, 156)
    lip = r(6, 0, 5)
    neck = r(7, 92, 104)
    beard = h[8] % 3 == 0
    tilt = r(9, -4, 4)
    d = [f"M30 238 C58 208 70 196 {neck-24:.0f} 168",
         f"C38 146 28 96 58 56 C88 {crown:.0f} 138 {crown+4:.0f} {fore:.0f} 66",
         f"C{fore+4:.0f} 82 {fore-2+brow:.0f} 92 {fore+brow:.0f} 104",
         f"C{fore+brow+4:.0f} 112 {fore+nose+8:.0f} 126 {fore+nose:.0f} {140+tilt:.0f}",
         f"C{fore+nose-4:.0f} 146 {fore-4:.0f} 148 {fore:.0f} 152",
         f"L{fore+lip+1:.0f} 158 L{fore-2:.0f} 162",
         f"C{fore-4:.0f} 168 {fore+lip:.0f} 172 {fore-2:.0f} 180"]
    if beard:
        d.append(f"C{fore-6:.0f} 200 {chin_x:.0f} 214 {chin_x-26:.0f} 218 "
                 f"C{chin_x-40:.0f} 214 {neck+4:.0f} 206 {neck:.0f} 192")
    else:
        d.append(f"C{fore-6:.0f} {chin-4:.0f} {chin_x+8:.0f} {chin:.0f} {chin_x-14:.0f} {chin:.0f} "
                 f"C{neck+6:.0f} {chin-2:.0f} {neck:.0f} 192 {neck:.0f} 184")
    d.append(f"C{neck+2:.0f} 204 116 218 160 236")
    path = " ".join(d)
    return Markup(
        f'<svg class="portrait" viewBox="0 0 200 244" width="{size}" height="{int(size*1.22)}" '
        f'role="img" aria-label="Портрет (линия)"><path d="{path}" fill="none" '
        f'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>')


# Готовые рисунки: имя автора -> файл в static/portraits/.
# Чтобы добавить портрет, положите PNG в эту папку и впишите строку сюда.
PORTRAIT_FILES = {
    'Сократ': 'sokrat.png',
    'Аристотель': 'aristotel.png',
    'Гераклит': 'geraklit.png',
    'Эпикур': 'epikur.png',
    'Конфуций': 'konfuciy.png',
    'Лао-цзы': 'laotszy.png',
    'Мэн-цзы': 'menczy.png',
    'Плавт': 'plavt.png',
    'Екклесиаст': 'ekklesiast.png',
    'Сенека': 'seneka.png',
    'Эпиктет': 'epiktet.png',
    'Плутарх': 'plutarh.png',
    'Марк Аврелий': 'mark-avrelij.png',
    'Аврелий Августин': 'avgustin.png',
    'Никколо Макиавелли': 'makiavelli.png',
    'Мишель де Монтень': 'monten.png',
    'Фрэнсис Бэкон': 'bekon.png',
    'Уильям Шекспир': 'shekspir.png',
    'Джон Донн': 'donn.png',
    'Рене Декарт': 'dekart.png',
    'Франсуа де Ларошфуко': 'laroshfuko.png',
    'Блез Паскаль': 'paskal.png',
    'Бенедикт Спиноза': 'spinoza.png',
    'Шарль де Монтескьё': 'monteske.png',
    'Бенджамин Франклин': 'franklin.png',
    'Жан-Жак Руссо': 'russo.png',
    'Вольтер': 'volter.png',
    'Иммануил Кант': 'kant.png',
    'Иоганн Вольфганг Гёте': 'gete.png',
    'Артур Шопенгауэр': 'shopengauer.png',
    'Ральф Уолдо Эмерсон': 'emerson.png',
    'Генри Дэвид Торо': 'toro.png',
    'Фёдор Достоевский': 'dostoevskij.png',
    'Платон': 'platon.png',
    'Джон Эмерих Эдвард Дальберг-Актон': 'akton.png',
    'Лев Толстой': 'tolstoj.png',
    'Фридрих Ницше': 'nicshe.png',
    'Антон Чехов': 'chehov.png',
    'Древнеегипетские авторы': 'egipet.png',
    'Гомер': 'gomer.png',
    'Книга притчей Соломоновых': 'pritchi.png',
    'Фалес': 'fales.png',
    'Эзоп': 'ezop.png',
    'Пифагор': 'pifagor.png',
    'Книга Бытия': 'bytie.png',
    'У-цзы': 'uczy.png',
    'Эсхил': 'eshil.png',
    'Геродот': 'gerodot.png',
    'Софокл': 'sofokl.png',
    '«Авеста»': 'avesta.png',
    'Сунь-цзы': 'sunczy.png',
    'Еврипид': 'evripid.png',
    'Гиппократ': 'gippokrat.png',
    'Аристофан': 'aristofan.png',
    'Демокрит': 'demokrit.png',
    'Диоген Синопский': 'diogen.png',
    '«Джатаки»': 'dzhataki.png',
    '«Дхаммапада»': 'dhammapada.png',
    'Книга премудрости Иисуса, сына Сирахова': 'sirah.png',
    'Катон Старший': 'katon.png',
    'Теренций': 'terencij.png',
    'Цицерон': 'ciceron.png',
    'Юлий Цезарь': 'czezar.png',
    'Вергилий': 'vergilij.png',
    'Гораций': 'goracij.png',
    'Тит Ливий': 'tit-livij.png',
    'Евангелие от Марка': 'ev-mark.png',
    'Марциал': 'marcial.png',
    'Евангелие от Луки': 'ev-luka.png',
    'Евангелие от Матфея': 'ev-matfej.png',
    'Тацит': 'tacit.png',
    'Плиний Младший': 'plinij.png',
    'Ювенал': 'yuvenal.png',
    'Евангелие от Иоанна': 'ev-ioann.png',
    'Апулей': 'apulej.png',
    'Василий Великий': 'vasilij-velikij.png',
    'Григорий Богослов': 'grigorij.png',
    'Иоанн Златоуст': 'zlatoust.png',
    'Юстиниан I Великий': 'yustinian.png',
    'Иоанн Дамаскин': 'damaskin.png',
    'Василий I Македонянин': 'vasilij-makedonyanin.png',
    'Мурасаки Сикибу': 'murasaki.png',
    'Авиценна': 'avicenna.png',
    'Маарри': 'maarri.png',
    'Кей-Кавус': 'kej-kavus.png',
    'Владимир Мономах': 'monomah.png',
    '«Пчела»': 'pchela.png',
    'Абу-ль-Фарадж': 'abul-faradzh.png',
    'Данте Алигьери': 'dante.png',
    'Франческо Петрарка': 'petrarka.png',
    'Джованни Боккаччо': 'bokkachcho.png',
    'Алишер Навои': 'navoi.png',
    'Леонардо да Винчи': 'leonardo.png',
    'Эразм Роттердамский': 'erazm.png',
    'Бабур': 'babur.png',
    'Томас Мор': 'mor.png',
    'Мартин Лютер': 'lyuter.png',
    'Маргарита Наваррская Старшая': 'margarita.png',
    'Даниель Дефо': 'defo.png',
    'Джонатан Свифт': 'svift.png',
    'Мигель де Сервантес': 'servantes.png',
    'Готфрид Лейбниц': 'lejbnic.png',
    'Дэвид Юм': 'yum.png',
    'Георг Гегель': 'gegel.png',
    'Сёрен Кьеркегор': 'kerkegor.png',
    'Зигмунд Фрейд': 'frejd.png',
    'Альберт Эйнштейн': 'ejnshtejn.png',
    'Галилео Галилей': 'galilej.png',
    'Виктор Гюго': 'gyugo.png',
    'Максим Горький': 'gorkij.png',
    'Генрих Гейне': 'gejne.png',
    'Фридрих Шиллер': 'shiller.png',
    'Карл Маркс': 'marks.png',
    'Оноре де Бальзак': 'balzak.png',
    'Александр Дюма-отец': 'dyuma.png',
    'Иван Бунин': 'bunin.png',
    'Николай Гоголь': 'gogol.png',
    'Иван Тургенев': 'turgenev.png',
    'Александр Пушкин': 'pushkin.png',
    'Михаил Лермонтов': 'lermontov.png',
    'Чарльз Диккенс': 'dikkens.png',
    'Марк Твен': 'tven.png',
    'Уильям Теккерей': 'tekkerej.png',
    'Оскар Уайльд': 'uajld.png',
    'Томас Манн': 'tmann.png',
    'Эрнест Хемингуэй': 'hemingujej.png',
    'Альбер Камю': 'kamyu.png',
    'Федор Карпов': 'karpov.png',
    'Иван Грозный': 'grozny.png',
    'Джордано Бруно': 'bruno.png',
    'Лопе де Вега': 'lope.png',
    'Хун Цзычэн': 'hun.png',
    'Бальтасар Грасиан': 'grasian.png',
    'Ян Амос Коменский': 'komenskij.png',
    'Педро Кальдерон де ла Барка': 'kalderon.png',
    'Джон Мильтон': 'milton.png',
    'Пьер Корнель': 'kornel.png',
    'Жан-Батист Мольер': 'moler.png',
    'Жан де Лафонтен': 'lafonten.png',
    'Жан Лабрюйер': 'labryujer.png',
    'Петр I Великий': 'petr1.png',
    'Михаил Ломоносов': 'lomonosov.png',
    'Дени Дидро': 'didro.png',
    'Адам Смит': 'smit.png',
    'Екатерина II': 'ekaterina.png',
    'Александр Суворов': 'suvorov.png',
    'Джордж Вашингтон': 'vashington.png',
    'Пьер Огюстен Бомарше': 'bomarshe.png',
    'Ричард Бринсли Шеридан': 'sheridan.png',
    'Томас Джефферсон': 'dzheferson.png',
    'Наполеон Бонапарт': 'napoleon.png',
    'Николай Карамзин': 'karamzin.png',
    'Вальтер Скотт': 'skott.png',
    'Джордж Байрон': 'bajron.png',
    'Александр Грибоедов': 'griboedov.png',
    'Петр Чаадаев': 'chaadaev.png',
    'Авраам Линкольн': 'linkoln.png',
    'Людвиг Фейербах': 'fejerbah.png',
    'Томас Карлейль': 'karlejl.png',
    'Ханс Кристиан Андерсен': 'andersen.png',
    'Бенджамин Дизраэли': 'dizraeli.png',
    'Гюстав Флобер': 'flober.png',
    'Козьма Прутков': 'prutkov.png',
    'Фридрих Энгельс': 'engels.png',
    'Эмиль Золя': 'zolya.png',
    'Роберт Льюис Стивенсон': 'stivenson.png',
    'Василий Ключевский': 'klyuchevskij.png',
    'Анатоль Франс': 'frans.png',
    'Василий Розанов': 'rozanov.png',
    'Теодор Рузвельт': 'ruzvelt.png',
    'Джек Лондон': 'london.png',
    'Владимир Ленин': 'lenin.png',
    'Александр Блок': 'blok.png',
    'Константин Станиславский': 'stanislavskij.png',
    'Рабиндранат Тагор': 'tagor.png',
    'Аркадий Аверченко': 'averchenko.png',
    'Джордж Бернард Шоу': 'shou.png',
    'Франц Кафка': 'kafka.png',
    'Ярослав Гашек': 'gashek.png',
    'Ромен Роллан': 'rollan.png',
    'Махатма Ганди': 'gandi.png',
    'Генрих Манн': 'gmann.png',
    'Николай Бердяев': 'berdyaev.png',
    'Стефан Цвейг': 'czvejg.png',
    'Михаил Пришвин': 'prishvin.png',
    'Карел Чапек': 'chapek.png',
    'Михаил Булгаков': 'bulgakov.png',
    'Уинстон Черчилль': 'cherchill.png',
    'Лион Фейхтвангер': 'fejhtvanger.png',
    'Антуан де Сент-Экзюпери': 'sent-ekzyuperi.png',
    'Андре Моруа': 'morua.png',
    'Джавахарлал Неру': 'neru.png',
    'Эрих Мария Ремарк': 'remark.png',
    'Джон Кеннеди': 'kennedi.png',
    'Хорхе Луис Борхес': 'borhes.png',
    'Мартин Лютер Кинг': 'king.png',
    'Хулио Кортасар': 'kortasar.png',
    'Индира Ганди': 'indira.png',
}
_author_names = {}


def portrait(author_id, size=160):
    if not _author_names:
        for r in q("SELECT id, name FROM authors"):
            _author_names[r["id"]] = r["name"]
    fn = PORTRAIT_FILES.get(_author_names.get(author_id, ""))
    if fn and os.path.exists(os.path.join(BASE, "static", "portraits", fn)):
        return Markup(f'<img class="portrait art" src="{url_for("static", filename="portraits/" + fn)}" '
                      f'alt="{escape(_author_names[author_id])}" height="{int(size*0.8)}" loading="lazy">')
    return portrait_svg(author_id, size)


app.jinja_env.globals["portrait"] = portrait


# ───────────────────────── запросы к афоризмам ─────────────────────────
APH_SELECT = """
SELECT a.*, au.name AS author_name,
  (SELECT COUNT(*) FROM reactions r WHERE r.aphorism_id=a.id) AS n_react,
  (SELECT COUNT(*) FROM comments c WHERE c.aphorism_id=a.id) AS n_comm
FROM aphorisms a JOIN authors au ON au.id=a.author_id
"""


# Эпохи — как в самой книге; границы приблизительные (год «для сортировки» — середина жизни автора)
ERAS = [
    ("ancient", "Древний мир", None, 500),
    ("early", "Раннее Средневековье", 500, 1000),
    ("medieval", "Средневековье", 1000, 1500),
    ("modern", "Новое время", 1500, 1900),
    ("newest", "Новейшее время", 1900, None),
]


def era_clause(era):
    for key, _, lo, hi in ERAS:
        if key == era:
            parts, params = [], []
            if lo is not None:
                parts.append("a.year >= ?"); params.append(lo)
            if hi is not None:
                parts.append("a.year < ?"); params.append(hi)
            return " AND ".join(parts), params
    return "", []


def with_era(where, params, era):
    sql, p = era_clause(era)
    if sql:
        return f"({where}) AND {sql}", list(params) + p
    return where, list(params)


def aphorism_list(where="1=1", params=(), sort="activity", page=1):
    order = {
        "activity": "(n_react + 2*n_comm) DESC, a.id",
        "author": "au.name, a.id",
        "time": "a.year, a.id",
        "time_desc": "a.year DESC, a.id",
        "theme": ("COALESCE((SELECT MIN(COALESCE(s.sort, 99) * 1000 + t.sort) FROM aphorism_themes x "
                  "JOIN themes t ON t.id=x.theme_id LEFT JOIN sections s ON s.id=t.section_id "
                  "WHERE x.aphorism_id=a.id), 99999), a.id"),
    }.get(sort, "a.id")
    sql = f"{APH_SELECT} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?"
    rows = q(sql, *params, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE)
    more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    return attach_themes(rows), more


def themes_by_section():
    """Список разделов, в каждом — его темы (для навигации, фильтров и выбора темы). Темы без раздела — в конце."""
    groups, index = [], {}
    for r in q("""SELECT t.slug, t.name, s.slug AS sec_slug, s.name AS sec_name FROM themes t
                  LEFT JOIN sections s ON s.id=t.section_id
                  ORDER BY COALESCE(s.sort, 999), t.sort, t.name"""):
        key = r["sec_slug"] or ""
        if key not in index:
            index[key] = dict(slug=r["sec_slug"], name=r["sec_name"] or "Другие темы", themes=[])
            groups.append(index[key])
        index[key]["themes"].append(dict(slug=r["slug"], name=r["name"]))
    return groups


def theme_filter(selected, mode):
    """selected — список slug'ов (или 'none' = без темы); mode: any — любая из выбранных, all — все сразу."""
    sel = [x for x in selected if x == "none" or q1("SELECT 1 FROM themes WHERE slug=?", x)]
    if not sel:
        return sel, "1=1", []
    if "none" in sel:
        return sel, "a.id NOT IN (SELECT aphorism_id FROM aphorism_themes)", []
    sub = "a.id IN (SELECT x.aphorism_id FROM aphorism_themes x JOIN themes t ON t.id=x.theme_id WHERE t.slug IN (%s))"
    if mode == "all":
        return sel, " AND ".join(sub % "?" for _ in sel), list(sel)
    return sel, sub % ",".join("?" for _ in sel), list(sel)


def list_page(endpoint, title, base_where, base_params, slug=None, theme_row=None, locked=(), crumb=None, only_section=None):
    """Общая страница списка: поиск, эпоха, темы, порядок. locked — темы, которые нельзя снять (страница темы)."""
    qs = (request.args.get("q") or "").strip()          # общий поиск из шапки: текст или автор
    f_text = (request.args.get("text") or "").strip()   # фильтр: слова в тексте
    f_au = (request.args.get("au") or "").strip()       # фильтр: автор
    sort = request.args.get("sort", "activity")
    era = request.args.get("era", "")
    page = page_arg()
    mode = "all" if (request.args.get("mode") == "all" or locked) else "any"
    extra = [x for x in request.args.getlist("t") if x not in locked]
    sel, tw, tp = theme_filter(list(locked) + extra, mode)
    where, params = base_where, list(base_params)
    for val, cond, n in ((qs, "(ci(a.text) LIKE ? OR ci(au.name) LIKE ?)", 2), (f_text, "ci(a.text) LIKE ?", 1),
                         (f_au, "ci(au.name) LIKE ?", 1)):
        if val:
            where, params = f"({where}) AND {cond}", params + [f"%{val.casefold()}%"] * n
    if tw != "1=1":
        where, params = f"({where}) AND {tw}", params + tp
    where, params = with_era(where, params, era)
    items, more = aphorism_list(where, params, sort, page)

    def link(**over):
        p = {"sort": sort, "era": era or None, "q": qs or None, "t": extra or None,
             "text": f_text or None, "au": f_au or None,
             "mode": "all" if (mode == "all" and not locked) else None}
        if slug:
            p["slug"] = slug
        p.update(over)
        return url_for(endpoint, **p)

    def chip(t):
        on = t["slug"] in extra
        return dict(name=t["name"], on=on,
                    href=link(t=[x for x in extra if x != t["slug"]] if on else extra + [t["slug"]], page=None))

    chip_groups = []
    for grp in themes_by_section():
        if only_section and grp["slug"] != only_section:
            continue
        ts = [chip(t) for t in grp["themes"] if t["slug"] not in locked]
        if ts:
            chip_groups.append(dict(name=grp["name"], chips=ts))
    none_on = "none" in extra
    if not only_section:
        chip_groups.append(dict(name="", chips=[dict(name="без темы", on=none_on,
                                                     href=link(t=None if none_on else ["none"], page=None))]))
    authors_all = [r["name"] for r in q("SELECT name FROM authors ORDER BY name")]
    return render_template("list.html", title=title, theme=theme_row, items=items, more=more, sort=sort,
                           page=page, query=qs, f_text=f_text, f_au=f_au, era=era, eras=ERAS, link=link,
                           chip_groups=chip_groups, crumb=crumb, n_sel=len(extra), mode=mode, locked=bool(locked), extra=extra,
                           form_action=url_for(endpoint, **({"slug": slug} if slug else {})),
                           authors_all=authors_all)


def attach_themes(rows):
    out = []
    for r in rows:
        d = dict(r)
        d["themes"] = q("""SELECT t.slug, t.name FROM themes t JOIN aphorism_themes x ON x.theme_id=t.id
                           WHERE x.aphorism_id=? ORDER BY t.name""", r["id"])
        d["reactions"] = reaction_counts(r["id"])
        mine = None
        if g.get("user"):
            m = q1("SELECT code FROM reactions WHERE user_id=? AND aphorism_id=?", g.user["id"], r["id"])
            mine = m["code"] if m else None
        d["mine"] = mine
        out.append(d)
    return out


def get_aphorism(aid):
    r = q1(APH_SELECT + " WHERE a.id=?", aid)
    if not r:
        abort(404)
    return attach_themes([r])[0]


def reaction_counts(aid):
    return q("""SELECT t.code, t.emoji, t.label,
                  (SELECT COUNT(*) FROM reactions r WHERE r.aphorism_id=? AND r.code=t.code) AS n
                FROM reaction_types t WHERE t.active=1 ORDER BY t.sort""", aid)


# ───────────────────────── главная: «пульс» ─────────────────────────
def pulse(days=7):
    now = dt.datetime.utcnow()
    cur_from = (now - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    prev_from = (now - dt.timedelta(days=2 * days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = q("""SELECT t.slug, t.name,
        (SELECT COUNT(DISTINCT user_id) FROM events e WHERE e.theme_id=t.id AND e.created_at>=?) AS cur,
        (SELECT COUNT(DISTINCT user_id) FROM events e WHERE e.theme_id=t.id AND e.created_at>=? AND e.created_at<?) AS prev
        FROM themes t""", cur_from, prev_from, cur_from)
    out = []
    for r in rows:
        if r["prev"]:
            pct = round((r["cur"] - r["prev"]) * 100 / r["prev"])
        else:
            pct = None
        out.append(dict(slug=r["slug"], name=r["name"], cur=r["cur"], prev=r["prev"], pct=pct,
                        new=(r["prev"] == 0 and r["cur"] > 0)))
    out.sort(key=lambda x: (-x["cur"], x["name"]))
    return out


def personal_aphorism():
    """Свой «афоризм дня»: зависит от человека и даты, ничего не навязывает."""
    n = q1("SELECT COUNT(*) c FROM aphorisms")["c"]
    if not n:
        return None
    key = f"{g.user['id'] if g.user else request.remote_addr}-{dt.date.today()}"
    idx = int(hashlib.sha256(key.encode()).hexdigest(), 16) % n
    r = q1(APH_SELECT + " ORDER BY a.id LIMIT 1 OFFSET ?", idx)
    return attach_themes([r])[0]


@app.route("/")
def home():
    p = pulse()
    since = (dt.datetime.utcnow() - dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    top = attach_themes(q(APH_SELECT + " ORDER BY (n_react + 2*n_comm) DESC, a.id LIMIT 5"))
    top = [a for a in top if a["n_react"] + a["n_comm"] > 0]
    activity = dict(
        people=q1("SELECT COUNT(DISTINCT user_id) c FROM events WHERE created_at>=?", since)["c"],
        thoughts=q1("SELECT COUNT(*) c FROM thoughts WHERE status='published' AND published_at>=?", since)["c"],
        comments=q1("SELECT COUNT(*) c FROM comments WHERE created_at>=?", since)["c"],
    )
    kin_act = None
    if g.user:
        ids = kin_ids(g.user["id"])
        if ids:
            marks = ",".join("?" * len(ids))
            kin_act = q(f"""SELECT u.username, u.display_name, th.id, th.published_at
                FROM thoughts th JOIN users u ON u.id=th.user_id
                WHERE th.user_id IN ({marks}) AND th.status='published' ORDER BY th.published_at DESC LIMIT 5""", *ids)
    return render_template("home.html", pulse=p[:12], top=top, activity=activity,
                           mine=personal_aphorism(), kin_act=kin_act)


# ───────────────────────── темы, афоризмы, авторы ─────────────────────────
@app.route("/themes")
def themes():
    counts = {r["slug"]: r["n"] for r in q("""SELECT t.slug, COUNT(x.aphorism_id) AS n FROM themes t
                LEFT JOIN aphorism_themes x ON x.theme_id=t.id GROUP BY t.id""")}
    sec_counts = {r["slug"]: r["n"] for r in q("""SELECT s.slug, COUNT(DISTINCT x.aphorism_id) AS n FROM sections s
                JOIN themes t ON t.section_id=s.id JOIN aphorism_themes x ON x.theme_id=t.id GROUP BY s.id""")}
    groups = themes_by_section()
    for g_ in groups:
        g_["n"] = sec_counts.get(g_["slug"])
        for t in g_["themes"]:
            t["n"] = counts.get(t["slug"], 0)
    return render_template("themes.html", groups=groups)


def page_arg():
    try:
        return max(1, int(request.args.get("page", 1)))
    except ValueError:
        return 1


@app.route("/t/<slug>")
def theme(slug):
    t = q1("SELECT t.*, s.slug AS sec_slug, s.name AS sec_name FROM themes t "
           "LEFT JOIN sections s ON s.id=t.section_id WHERE t.slug=?", slug)
    if not t:
        abort(404)
    crumb = dict(name=t["sec_name"], url=url_for("section", slug=t["sec_slug"])) if t["sec_slug"] else None
    return list_page("theme", t["name"], "1=1", [], slug=slug, theme_row=t, locked=(slug,), crumb=crumb)


@app.route("/s/<slug>")
def section(slug):
    s = q1("SELECT * FROM sections WHERE slug=?", slug)
    if not s:
        abort(404)
    where = ("a.id IN (SELECT x.aphorism_id FROM aphorism_themes x JOIN themes t ON t.id=x.theme_id "
             "WHERE t.section_id=?)")
    return list_page("section", s["name"], where, [s["id"]], slug=slug, only_section=slug,
                     crumb=dict(name="Все темы", url=url_for("themes")))


@app.route("/aphorisms")
def aphorisms():
    searching = any((request.args.get(k) or "").strip() for k in ("q", "text", "au"))
    return list_page("aphorisms", "Поиск" if searching else "Все афоризмы", "1=1", [])


@app.route("/eras")
def eras_page():
    rows = []
    for key, name, lo, hi in ERAS:
        sql, p = era_clause(key)
        n = q1(f"SELECT COUNT(*) c FROM aphorisms a WHERE {sql}", *p)["c"]
        na = q1(f"SELECT COUNT(DISTINCT author_id) c FROM aphorisms a WHERE {sql}", *p)["c"]
        span = ("до %d г." % hi) if lo is None else (("с %d г." % lo) if hi is None else "%d–%d" % (lo, hi))
        rows.append(dict(key=key, name=name, span=span, n=n, authors=na))
    return render_template("eras.html", eras=rows)


@app.route("/authors")
def authors():
    sort = request.args.get("sort", "time")
    order = {"name": "au.name COLLATE NOCASE", "count": "n DESC, au.name"}.get(sort, "y, au.name")
    rows = q(f"""SELECT au.*, (SELECT COUNT(*) FROM aphorisms a WHERE a.author_id=au.id) AS n,
                (SELECT MIN(year) FROM aphorisms a WHERE a.author_id=au.id) AS y
                FROM authors au ORDER BY {order}""")
    return render_template("authors.html", authors=rows, sort=sort)


@app.route("/author/<int:aid>")
def author(aid):
    au = q1("SELECT * FROM authors WHERE id=?", aid)
    if not au:
        abort(404)
    page = page_arg()
    items, more = aphorism_list("a.author_id=?", (aid,), "time", page)
    total = q1("SELECT COUNT(*) c FROM aphorisms WHERE author_id=?", aid)["c"]
    return render_template("author.html", au=au, items=items, more=more, page=page, total=total)


@app.route("/author/<int:aid>/about")
def author_about(aid):
    au = q1("SELECT * FROM authors WHERE id=?", aid)
    if not au:
        abort(404)
    achievements = [x for x in (au["achievements"] or "").split("\n") if x.strip()]
    total = q1("SELECT COUNT(*) c FROM aphorisms WHERE author_id=?", aid)["c"]
    return render_template("author_about.html", au=au, achievements=achievements, total=total)


@app.route("/a/<int:aid>")
def aphorism(aid):
    a = get_aphorism(aid)
    mine = None
    if g.user:
        r = q1("SELECT code FROM reactions WHERE user_id=? AND aphorism_id=?", g.user["id"], aid)
        mine = r["code"] if r else None
    comments = q("""SELECT c.*, u.username, u.display_name FROM comments c JOIN users u ON u.id=c.user_id
                    WHERE c.aphorism_id=? ORDER BY c.created_at, c.id""", aid)
    kin = set(kin_ids(g.user["id"])) if g.user else set()
    # другие мысли людей, связанные с этим афоризмом (только открытые)
    thoughts = q("""SELECT th.id, th.body, th.published_at, u.username, u.display_name
                    FROM thoughts th JOIN thought_aphorisms ta ON ta.thought_id=th.id
                    JOIN users u ON u.id=th.user_id
                    WHERE ta.aphorism_id=? AND th.status='published' AND th.kin_only=0
                    ORDER BY th.published_at DESC LIMIT 5""", aid)
    return render_template("aphorism.html", a=a, theme_groups=themes_by_section(), reactions=reaction_counts(aid), mine=mine,
                           comments=comments, kin=kin, thoughts=thoughts,
                           in_basket=aid in session.get("compare", []))


_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a","b","v","g","d","e","e","zh","z","i","y","k","l","m","n","o","p","r","s","t","u","f","h","c",
                "ch","sh","sch","","y","","e","yu","ya"]))


def make_slug(name):
    base = "".join(_TR.get(ch, ch) for ch in name.lower())
    base = "-".join("".join(c for c in base if c.isalnum() or c == " ").split()) or "tema"
    slug, n = base, 2
    while q1("SELECT 1 FROM themes WHERE slug=?", slug):
        slug, n = f"{base}-{n}", n + 1
    return slug


@app.post("/a/<int:aid>/themes")
@login_required
def aphorism_themes(aid):
    get_aphorism(aid)
    act = request.form.get("act", "")
    cur = {r["slug"]: r["id"] for r in q("""SELECT t.id, t.slug FROM themes t JOIN aphorism_themes x
                                            ON x.theme_id=t.id WHERE x.aphorism_id=?""", aid)}
    new_name = " ".join(request.form.get("new", "").split())[:40]
    target = None
    if new_name:                                   # своя тема: найти по названию или создать
        row = next((t for t in q("SELECT id, slug, name FROM themes") if t["name"].casefold() == new_name.casefold()), None)
        if not row:
            if len(new_name) < 2:
                flash("Название темы слишком короткое.")
                return redirect(url_for("aphorism", aid=aid) + "#themes")
            slug = make_slug(new_name)
            ex("INSERT INTO themes(slug, name) VALUES (?,?)", slug, new_name[0].upper() + new_name[1:])
            row = q1("SELECT id, slug FROM themes WHERE slug=?", slug)
        target = row
    elif request.form.get("theme"):
        target = q1("SELECT id, slug FROM themes WHERE slug=?", request.form["theme"])
        if not target:
            abort(400)
    old = request.form.get("old", "")
    if act == "remove" and old in cur:
        ex("DELETE FROM aphorism_themes WHERE aphorism_id=? AND theme_id=?", aid, cur[old])
    elif act in ("add", "replace") and target:
        if act == "replace" and old in cur:
            ex("DELETE FROM aphorism_themes WHERE aphorism_id=? AND theme_id=?", aid, cur[old])
        ex("INSERT OR IGNORE INTO aphorism_themes(aphorism_id, theme_id) VALUES (?,?)", aid, target["id"])
        log_event([aid])
    else:
        abort(400)
    return redirect(url_for("aphorism", aid=aid) + "#themes")


# ───────────────────────── реакции и комментарии ─────────────────────────
@app.post("/a/<int:aid>/react")
@login_required
def react(aid):
    get_aphorism(aid)
    code = request.form.get("code", "")
    uid = g.user["id"]
    cur = q1("SELECT code FROM reactions WHERE user_id=? AND aphorism_id=?", uid, aid)
    if not code or (cur and cur["code"] == code):
        ex("DELETE FROM reactions WHERE user_id=? AND aphorism_id=?", uid, aid)   # снять реакцию
    else:
        if not q1("SELECT 1 FROM reaction_types WHERE code=? AND active=1", code):
            abort(400)
        ex("INSERT INTO reactions(user_id, aphorism_id, code) VALUES (?,?,?) "
           "ON CONFLICT(user_id, aphorism_id) DO UPDATE SET code=excluded.code", uid, aid, code)
        log_event([aid])
    back = request.form.get("back", "")
    if back.startswith("/") and not back.startswith("//"):
        return redirect(back)
    return redirect(url_for("aphorism", aid=aid) + "#reactions")


@app.post("/a/<int:aid>/comment")
@login_required
def comment_add(aid):
    get_aphorism(aid)
    body = request.form.get("body", "").strip()
    if not body:
        flash("Напишите хотя бы несколько слов.")
    elif len(body) > 4000:
        flash("Слишком длинно: сократите до 4000 знаков.")
    else:
        ex("INSERT INTO comments(aphorism_id, user_id, body) VALUES (?,?,?)", aid, g.user["id"], body)
        log_event([aid])
    return redirect(url_for("aphorism", aid=aid) + "#comments")


def own_comment(cid):
    c = q1("SELECT * FROM comments WHERE id=?", cid)
    if not c:
        abort(404)
    if c["user_id"] != g.user["id"]:
        abort(403)
    return c


@app.route("/comment/<int:cid>/edit", methods=["GET", "POST"])
@login_required
def comment_edit(cid):
    c = own_comment(cid)
    if request.method == "POST":
        body = request.form.get("body", "").strip()
        if body and len(body) <= 4000:
            ex("UPDATE comments SET body=?, edited_at=datetime('now') WHERE id=?", body, cid)
            return redirect(url_for("aphorism", aid=c["aphorism_id"]) + "#comments")
        flash("Текст не должен быть пустым или длиннее 4000 знаков.")
    return render_template("comment_edit.html", c=c, a=get_aphorism(c["aphorism_id"]))


@app.post("/comment/<int:cid>/delete")
@login_required
def comment_delete(cid):
    c = own_comment(cid)
    ex("DELETE FROM comments WHERE id=?", cid)
    return redirect(url_for("aphorism", aid=c["aphorism_id"]) + "#comments")


# ───────────────────────── сравнение ─────────────────────────
@app.post("/compare/add/<int:aid>")
def compare_add(aid):
    get_aphorism(aid)
    b = session.get("compare", [])
    if aid not in b:
        if len(b) >= MAX_COMPARE:
            flash(f"Для сравнения можно собрать до {MAX_COMPARE} афоризмов.")
        else:
            b.append(aid)
            log_event([aid])
    session["compare"] = b
    return redirect(request.form.get("back") or url_for("compare"))


@app.post("/compare/remove/<int:aid>")
def compare_remove(aid):
    session["compare"] = [x for x in session.get("compare", []) if x != aid]
    return redirect(request.form.get("back") or url_for("compare"))


@app.post("/compare/clear")
def compare_clear():
    session["compare"] = []
    return redirect(url_for("compare"))


@app.route("/compare")
def compare():
    ids = session.get("compare", [])
    items = []
    for i in ids:
        r = q1(APH_SELECT + " WHERE a.id=?", i)
        if r:
            items.append(attach_themes([r])[0])
    view = request.args.get("view", "side")
    # Намеренно: порядок — тот, в котором человек сам собрал афоризмы.
    return render_template("compare.html", items=items, view=view)


# ───────────────────────── собственные мысли ─────────────────────────
@app.post("/think/start")
@login_required
def think_start():
    ids = session.get("compare", [])
    if not ids:
        flash("Сначала соберите несколько афоризмов для сравнения.")
        return redirect(url_for("compare"))
    cur = ex("INSERT INTO thoughts(user_id) VALUES (?)", g.user["id"])
    tid = cur.lastrowid
    for i in ids:
        db().execute("INSERT OR IGNORE INTO thought_aphorisms VALUES (?,?)", (tid, i))
    db().commit()
    return redirect(url_for("thought_edit", tid=tid))


def own_thought(tid):
    t = q1("SELECT * FROM thoughts WHERE id=?", tid)
    if not t:
        abort(404)
    if t["user_id"] != g.user["id"]:
        abort(403)
    return t


def thought_sources(tid):
    return attach_themes(q(APH_SELECT + """ WHERE a.id IN
        (SELECT aphorism_id FROM thought_aphorisms WHERE thought_id=?) ORDER BY a.year""", tid))


@app.route("/think/<int:tid>/edit", methods=["GET", "POST"])
@login_required
def thought_edit(tid):
    t = own_thought(tid)
    if request.method == "POST":
        body = request.form.get("body", "").strip()
        kin_only = 1 if request.form.get("kin_only") else 0
        action = request.form.get("action", "save")
        if len(body) > 8000:
            flash("Слишком длинно: сократите до 8000 знаков.")
        elif action == "publish":
            if len(body) < 3:
                flash("Прежде чем опубликовать, сформулируйте мысль.")
            else:
                if t["status"] == "published":
                    ex("UPDATE thoughts SET body=?, kin_only=?, updated_at=datetime('now') WHERE id=?",
                       body, kin_only, tid)
                else:
                    ex("""UPDATE thoughts SET body=?, kin_only=?, status='published',
                          published_at=datetime('now') WHERE id=?""", body, kin_only, tid)
                    log_event([r["aphorism_id"] for r in
                               q("SELECT aphorism_id FROM thought_aphorisms WHERE thought_id=?", tid)])
                return redirect(url_for("thought", tid=tid))
        else:
            if t["status"] == "published":
                ex("UPDATE thoughts SET body=?, kin_only=?, updated_at=datetime('now') WHERE id=?",
                   body, kin_only, tid)
                return redirect(url_for("thought", tid=tid))
            ex("UPDATE thoughts SET body=?, kin_only=? WHERE id=?", body, kin_only, tid)
            flash("Черновик сохранён.")
            return redirect(url_for("thought_edit", tid=tid))
        t = dict(t, body=body, kin_only=kin_only)
    return render_template("thought_edit.html", t=t, sources=thought_sources(tid))


@app.post("/think/<int:tid>/delete")
@login_required
def thought_delete(tid):
    own_thought(tid)
    ex("DELETE FROM thoughts WHERE id=?", tid)
    flash("Мысль удалена.")
    return redirect(url_for("profile", username=g.user["username"]))


@app.route("/think/<int:tid>")
def thought(tid):
    t = q1("""SELECT th.*, u.username, u.display_name FROM thoughts th
              JOIN users u ON u.id=th.user_id WHERE th.id=?""", tid)
    if not t:
        abort(404)
    mine = g.user and g.user["id"] == t["user_id"]
    if t["status"] != "published" and not mine:
        abort(404)
    if t["kin_only"] and not (mine or (g.user and is_kin(g.user["id"], t["user_id"]))):
        abort(404)
    return render_template("thought.html", t=t, sources=thought_sources(tid), mine=mine)


# ───────────────────────── профили, интерес, родные ─────────────────────────
@app.route("/u/<username>")
def profile(username):
    u = q1("SELECT * FROM users WHERE username=?", username)
    if not u:
        abort(404)
    me_id = g.user["id"] if g.user else None
    mine = me_id == u["id"]
    kin = mine or is_kin(me_id, u["id"])
    sql = "SELECT * FROM thoughts WHERE user_id=? AND status='published'"
    if not kin:
        sql += " AND kin_only=0"
    thoughts = q(sql + " ORDER BY published_at DESC", u["id"])
    drafts = q("SELECT * FROM thoughts WHERE user_id=? AND status='draft' ORDER BY id DESC", u["id"]) if mine else []
    n_interest = q1("SELECT COUNT(*) c FROM interests WHERE target_id=?", u["id"])["c"]
    kin_interested = []
    if mine:
        kin_interested = q("""SELECT u2.username, u2.display_name FROM interests i JOIN users u2 ON u2.id=i.user_id
                              WHERE i.target_id=?""", u["id"])
        kin_interested = [k for k in kin_interested
                          if is_kin(me_id, q1("SELECT id FROM users WHERE username=?", k["username"])["id"])]
    interested = bool(me_id) and q1("SELECT 1 FROM interests WHERE user_id=? AND target_id=?", me_id, u["id"]) is not None
    reacted = q1("SELECT COUNT(*) c FROM reactions WHERE user_id=?", u["id"])["c"]
    return render_template("profile.html", u=u, thoughts=thoughts, drafts=drafts, mine=mine, kin=kin and not mine,
                           n_interest=n_interest, kin_interested=kin_interested, interested=interested,
                           reacted=reacted)


@app.post("/u/<username>/interest")
@login_required
def interest_toggle(username):
    u = q1("SELECT id FROM users WHERE username=?", username)
    if not u or u["id"] == g.user["id"]:
        abort(400)
    if q1("SELECT 1 FROM interests WHERE user_id=? AND target_id=?", g.user["id"], u["id"]):
        ex("DELETE FROM interests WHERE user_id=? AND target_id=?", g.user["id"], u["id"])
    else:
        ex("INSERT INTO interests VALUES (?,?)", g.user["id"], u["id"])
    return redirect(url_for("profile", username=username))


@app.route("/me/interests")
@login_required
def my_interests():
    rows = q("""SELECT u.username, u.display_name,
                (SELECT COUNT(*) FROM thoughts t WHERE t.user_id=u.id AND t.status='published' AND t.kin_only=0) AS n
                FROM interests i JOIN users u ON u.id=i.target_id WHERE i.user_id=? ORDER BY u.display_name""",
             g.user["id"])
    return render_template("interests.html", rows=rows)


@app.route("/kin")
@login_required
def kin_page():
    uid = g.user["id"]
    rows = q("""SELECT k.*, u.username, u.display_name FROM kin k
                JOIN users u ON u.id = CASE WHEN k.user_a=? THEN k.user_b ELSE k.user_a END
                WHERE k.user_a=? OR k.user_b=?""", uid, uid, uid)
    confirmed = [r for r in rows if r["status"] == "confirmed"]
    incoming = [r for r in rows if r["status"] == "pending" and r["requested_by"] != uid]
    outgoing = [r for r in rows if r["status"] == "pending" and r["requested_by"] == uid]
    return render_template("kin.html", confirmed=confirmed, incoming=incoming, outgoing=outgoing)


@app.post("/kin/propose")
@login_required
def kin_propose():
    name = request.form.get("username", "").strip().lstrip("@").lower()
    relation = request.form.get("relation", "").strip()[:40]
    u = q1("SELECT id FROM users WHERE username=?", name)
    if not u:
        flash("Такого пользователя нет.")
    elif u["id"] == g.user["id"]:
        flash("Это вы сами.")
    else:
        a, b = kin_pair(g.user["id"], u["id"])
        if q1("SELECT 1 FROM kin WHERE user_a=? AND user_b=?", a, b):
            flash("Связь уже предложена или установлена.")
        else:
            ex("INSERT INTO kin(user_a, user_b, requested_by, relation) VALUES (?,?,?,?)",
               a, b, g.user["id"], relation)
            flash("Предложение отправлено. Связь появится, когда человек её подтвердит.")
    return redirect(url_for("kin_page"))


@app.post("/kin/<username>/<action>")
@login_required
def kin_act(username, action):
    u = q1("SELECT id FROM users WHERE username=?", username)
    if not u:
        abort(404)
    a, b = kin_pair(g.user["id"], u["id"])
    row = q1("SELECT * FROM kin WHERE user_a=? AND user_b=?", a, b)
    if not row:
        abort(404)
    if action == "confirm" and row["status"] == "pending" and row["requested_by"] != g.user["id"]:
        ex("UPDATE kin SET status='confirmed' WHERE user_a=? AND user_b=?", a, b)
    elif action in ("decline", "remove"):
        ex("DELETE FROM kin WHERE user_a=? AND user_b=?", a, b)
    else:
        abort(400)
    return redirect(url_for("kin_page"))


# ───────────────────────── вход и регистрация ─────────────────────────
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        name = request.form.get("display_name", "").strip()
        pw = request.form.get("password", "")
        ok = (3 <= len(username) <= 30 and all(c.isalnum() or c in "_-" for c in username)
              and name and len(pw) >= 8)
        if not ok:
            flash("Логин: 3–30 букв, цифр, «_» или «-». Укажите имя. Пароль — от 8 знаков.")
        elif q1("SELECT 1 FROM users WHERE username=?", username):
            flash("Такой логин уже занят.")
        else:
            cur = ex("INSERT INTO users(username, pass_hash, display_name) VALUES (?,?,?)",
                     username, generate_password_hash(pw), name[:60])
            session.clear()
            session["uid"] = cur.lastrowid
            return redirect(url_for("home"))
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q1("SELECT * FROM users WHERE username=?", request.form.get("username", "").strip().lower())
        if u and check_password_hash(u["pass_hash"], request.form.get("password", "")):
            compare = session.get("compare", [])
            session.clear()
            session["uid"] = u["id"]
            session["compare"] = compare
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("home"))
        flash("Неверный логин или пароль.")
    return render_template("auth.html", mode="login")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        ex("UPDATE users SET display_name=?, bio=? WHERE id=?",
           request.form.get("display_name", "").strip()[:60] or g.user["display_name"],
           request.form.get("bio", "").strip()[:600], g.user["id"])
        flash("Сохранено.")
        return redirect(url_for("profile", username=g.user["username"]))
    return render_template("settings.html")


@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(400)
def err(e):
    return render_template("error.html", e=e), e.code


if __name__ == "__main__":
    init_db()
    apply_authors_info()
    apply_taxonomy()
    app.run(debug=False, host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
