PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  pass_hash TEXT NOT NULL,
  display_name TEXT NOT NULL,
  bio TEXT NOT NULL DEFAULT '',
  is_demo INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS authors (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  years TEXT NOT NULL,
  bio TEXT NOT NULL,
  achievements TEXT NOT NULL DEFAULT ''   -- по одному достижению на строку
);

-- Разделы нужны только для удобной навигации; афоризмам назначаются темы (подтемы разделов).
CREATE TABLE IF NOT EXISTS sections (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  sort INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS themes (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  section_id INTEGER REFERENCES sections(id) ON DELETE SET NULL,
  sort INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS aphorisms (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
  author_id INTEGER NOT NULL REFERENCES authors(id),
  year INTEGER,                 -- отрицательное = до н.э.
  year_note TEXT NOT NULL DEFAULT '',   -- «ок.», «приписывается» и т.п.
  source TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS aphorism_themes (
  aphorism_id INTEGER NOT NULL REFERENCES aphorisms(id) ON DELETE CASCADE,
  theme_id INTEGER NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
  PRIMARY KEY (aphorism_id, theme_id)
);

-- Набор реакций хранится в таблице: его легко менять без правки кода.
CREATE TABLE IF NOT EXISTS reaction_types (
  code TEXT PRIMARY KEY,
  emoji TEXT NOT NULL,
  label TEXT NOT NULL,
  sort INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);

-- Одна текущая реакция на пару (человек, афоризм). Истории нет.
CREATE TABLE IF NOT EXISTS reactions (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  aphorism_id INTEGER NOT NULL REFERENCES aphorisms(id) ON DELETE CASCADE,
  code TEXT NOT NULL REFERENCES reaction_types(code),
  PRIMARY KEY (user_id, aphorism_id)
);

CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY,
  aphorism_id INTEGER NOT NULL REFERENCES aphorisms(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  edited_at TEXT
);

-- Собственная мысль. status: draft | published.
-- Пока draft — body это черновик; после публикации — опубликованный текст.
CREATE TABLE IF NOT EXISTS thoughts (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  body TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  kin_only INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  published_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS thought_aphorisms (
  thought_id INTEGER NOT NULL REFERENCES thoughts(id) ON DELETE CASCADE,
  aphorism_id INTEGER NOT NULL REFERENCES aphorisms(id) ON DELETE CASCADE,
  PRIMARY KEY (thought_id, aphorism_id)
);

-- «Интересуюсь этим человеком». Закрытый список.
CREATE TABLE IF NOT EXISTS interests (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  target_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  PRIMARY KEY (user_id, target_id),
  CHECK (user_id <> target_id)
);

-- Родственные связи. user_a < user_b. Действует только status = 'confirmed'.
CREATE TABLE IF NOT EXISTS kin (
  user_a INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  user_b INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  requested_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  relation TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  PRIMARY KEY (user_a, user_b),
  CHECK (user_a < user_b)
);

-- Журнал действий для «пульса» тем: кто, о какой теме, когда.
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  theme_id INTEGER NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ix_events_theme_time ON events(theme_id, created_at);
CREATE INDEX IF NOT EXISTS ix_comments_aph ON comments(aphorism_id);
CREATE INDEX IF NOT EXISTS ix_reactions_aph ON reactions(aphorism_id);
CREATE INDEX IF NOT EXISTS ix_thoughts_user ON thoughts(user_id, status);
