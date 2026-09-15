import os
import json
import random
from pathlib import Path
from collections import defaultdict

import telebot
from telebot import types
import psycopg
BASE_DIR = Path(__file__).resolve().parent
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")
bot = telebot.TeleBot(TOKEN)
def init_db():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    first_name TEXT,
                    last_name TEXT,
                    username TEXT,
                    registered_at TIMESTAMPTZ DEFAULT NOW(),
                    last_activity TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS free_questions_used INTEGER DEFAULT 0,
                ADD COLUMN IF NOT EXISTS premium_until TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS manual_access_until TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS total_answers INTEGER DEFAULT 0,
                ADD COLUMN IF NOT EXISTS correct_answers INTEGER DEFAULT 0,
                ADD COLUMN IF NOT EXISTS wrong_answers INTEGER DEFAULT 0;
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS answer_history (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    question_id INTEGER NOT NULL,
                    is_correct BOOLEAN NOT NULL,
                    answered_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        conn.commit()


init_db()
def save_user(user):
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (
                    telegram_id,
                    first_name,
                    last_name,
                    username,
                    last_activity
                )
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (telegram_id)
                DO UPDATE SET
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    username = EXCLUDED.username,
                    last_activity = NOW()
            """, (
                user.id,
                user.first_name,
                user.last_name,
                user.username
            ))
        conn.commit()
with open(BASE_DIR / "questions.json", "r", encoding="utf-8") as f:
    QUESTIONS = json.load(f)

BY_ID = {q["id"]: q for q in QUESTIONS}

sessions = {}
stats = defaultdict(lambda: {"correct": 0, "wrong": 0, "mistakes": set()})


def main_menu():
    kb = types.InlineKeyboardMarkup(row_width=2)

    kb.add(
        types.InlineKeyboardButton("📚 Раздел 1", callback_data="menu_section:1"),
        types.InlineKeyboardButton("📚 Раздел 2", callback_data="menu_section:2"),
        types.InlineKeyboardButton("📚 Раздел 3", callback_data="menu_section:3"),
    )

    kb.add(
        types.InlineKeyboardButton("🎯 Все вопросы", callback_data="start_all"),
        types.InlineKeyboardButton("❌ Мои ошибки", callback_data="start_mistakes"),
    )

    kb.add(
        types.InlineKeyboardButton("📝 Экзамен", callback_data="start_exam"),
        types.InlineKeyboardButton("📊 Статистика", callback_data="show_stats"),
    )

    return kb


def section_menu(root):
    sections = sorted({
        q["section"]
        for q in QUESTIONS
        if q["section"].startswith(root + ".")
    })

    kb = types.InlineKeyboardMarkup(row_width=2)

    for sec in sections:
        kb.add(
            types.InlineKeyboardButton(
                f"Раздел {sec}",
                callback_data=f"start_section:{sec}"
            )
        )

    kb.add(
        types.InlineKeyboardButton(
            "⬅️ Главное меню",
            callback_data="home"
        )
    )

    return kb


def answer_keyboard(qid):
    kb = types.InlineKeyboardMarkup(row_width=3)

    kb.add(
        types.InlineKeyboardButton("A", callback_data=f"ans:{qid}:A"),
        types.InlineKeyboardButton("B", callback_data=f"ans:{qid}:B"),
        types.InlineKeyboardButton("C", callback_data=f"ans:{qid}:C"),
    )

    return kb


def next_keyboard():
    kb = types.InlineKeyboardMarkup()

    kb.add(
        types.InlineKeyboardButton(
            "➡️ Следующий",
            callback_data="next"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "🏠 Главное меню",
            callback_data="home"
        )
    )

    return kb


def send_question(chat_id):
    s = sessions.get(chat_id)

    if not s:
        bot.send_message(
            chat_id,
            "Сначала выбери режим.",
            reply_markup=main_menu()
        )
        return

    if s["index"] >= len(s["queue"]):
        finish_session(chat_id)
        return

    q = BY_ID[s["queue"][s["index"]]]

    title = f"📚 Раздел {q['section']} • Вопрос {q.get('number', '')}"
    progress = f"{s['index'] + 1}/{len(s['queue'])}"

    text = (
        f"{title}\n"
        f"📌 {progress}\n\n"
        f"{q['question']}\n\n"
        f"A) {q['options']['A']}\n\n"
        f"B) {q['options']['B']}\n\n"
        f"C) {q['options']['C']}"
    )

    image = q.get("image")

    if image:
        img_path = BASE_DIR / image

        if img_path.exists():
            with open(img_path, "rb") as photo:
                bot.send_photo(chat_id, photo)

    bot.send_message(
        chat_id,
        text,
        reply_markup=answer_keyboard(q["id"])
    )


def start_session(chat_id, pool, mode="training", count=None):
    if not pool:
        bot.send_message(
            chat_id,
            "В этом режиме пока нет вопросов.",
            reply_markup=main_menu()
        )
        return

    pool = list(pool)
    random.shuffle(pool)

    if count:
        pool = pool[:min(count, len(pool))]

    sessions[chat_id] = {
        "mode": mode,
        "queue": [q["id"] for q in pool],
        "index": 0,
        "correct": 0,
        "wrong": 0,
        "exam_answers": [],
    }

    send_question(chat_id)


def finish_session(chat_id):
    s = sessions.get(chat_id)

    if not s:
        return

    total = len(s["queue"])
    correct = s["correct"]
    wrong = s["wrong"]

    pct = round(correct / total * 100, 1) if total else 0

    text = (
        "🏁 Тест завершён!\n\n"
        f"✅ Правильно: {correct}\n"
        f"❌ Ошибок: {wrong}\n"
        f"📊 Результат: {correct}/{total} ({pct}%)"
    )

    if s["mode"] == "exam" and s["exam_answers"]:
        bad = [
            x for x in s["exam_answers"]
            if not x["ok"]
        ]

        if bad:
            text += "\n\nОшибки экзамена:"

            for x in bad[:20]:
                text += (
                    f"\n• {x['section']} №{x['number']}: "
                    f"{x['selected']} → {x['correct']}"
                )

    sessions.pop(chat_id, None)

    bot.send_message(
        chat_id,
        text,
        reply_markup=main_menu()
    )


@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    save_user(message.from_user)
    bot.send_message(
        message.chat.id,
        "🚛 Code 95 Training Bot\n\n"
        "Выбери режим подготовки:",
        reply_markup=main_menu(),
    )


@bot.message_handler(commands=["test"])
def cmd_test(message):
    start_session(
        message.chat.id,
        QUESTIONS,
        mode="training",
        count=20
    )


@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    bot.answer_callback_query(call.id)

    chat_id = call.message.chat.id
    data = call.data

    if data == "home":
        sessions.pop(chat_id, None)

        bot.edit_message_text(
            "🚛 Code 95 Training Bot\n\nВыбери режим подготовки:",
            chat_id=chat_id,
            message_id=call.message.message_id,
            reply_markup=main_menu()
        )
        return

    if data.startswith("menu_section:"):
        root = data.split(":", 1)[1]

        bot.edit_message_text(
            f"📚 Выбери подраздел раздела {root}:",
            chat_id=chat_id,
            message_id=call.message.message_id,
            reply_markup=section_menu(root)
        )
        return

    if data.startswith("start_section:"):
        sec = data.split(":", 1)[1]

        pool = [
            q for q in QUESTIONS
            if q["section"] == sec
        ]

        start_session(
            chat_id,
            pool,
            mode="training"
        )
        return

    if data == "start_all":
        start_session(
            chat_id,
            QUESTIONS,
            mode="training"
        )
        return

    if data == "start_exam":
        start_session(
            chat_id,
            QUESTIONS,
            mode="exam",
            count=20
        )
        return

    if data == "start_mistakes":
        ids = stats[chat_id]["mistakes"]

        pool = [
            BY_ID[qid]
            for qid in ids
            if qid in BY_ID
        ]

        start_session(
            chat_id,
            pool,
            mode="training"
        )
        return

    if data == "show_stats":
        st = stats[chat_id]

        total = st["correct"] + st["wrong"]

        pct = (
            round(st["correct"] / total * 100, 1)
            if total else 0
        )

        bot.send_message(
            chat_id,
            "📊 Статистика\n\n"
            f"✅ Правильно: {st['correct']}\n"
            f"❌ Ошибок: {st['wrong']}\n"
            f"Всего ответов: {total}\n"
            f"Результат: {pct}%\n"
            f"Вопросов в «Мои ошибки»: {len(st['mistakes'])}",
            reply_markup=main_menu(),
        )
        return

    if data == "next":
        if chat_id not in sessions:
            bot.send_message(
                chat_id,
                "Сессия завершена.",
                reply_markup=main_menu()
            )
            return

        sessions[chat_id]["index"] += 1
        send_question(chat_id)
        return

    if data.startswith("ans:"):
        _, qid, selected = data.split(":", 2)

        s = sessions.get(chat_id)

        if not s or s["index"] >= len(s["queue"]):
            return

        current_id = s["queue"][s["index"]]

        if qid != current_id:
            return

        q = BY_ID[qid]
        correct = q["correct"]
        ok = selected == correct

        if ok:
            s["correct"] += 1
            stats[chat_id]["correct"] += 1

        else:
            s["wrong"] += 1
            stats[chat_id]["wrong"] += 1
            stats[chat_id]["mistakes"].add(qid)

        if s["mode"] == "exam":
            s["exam_answers"].append({
                "section": q["section"],
                "number": q.get("number", ""),
                "selected": selected,
                "correct": correct,
                "ok": ok,
            })

            s["index"] += 1
            send_question(chat_id)
            return

        if ok:
            result = "✅ Правильно!"
        else:
            result = (
                "❌ Неправильно.\n"
                f"Правильный ответ: {correct}"
            )

        bot.edit_message_reply_markup(
            chat_id,
            call.message.message_id,
            reply_markup=None
        )

        bot.send_message(
            chat_id,
            result,
            reply_markup=next_keyboard()
        )


print(f"Bot started. Loaded {len(QUESTIONS)} questions.")
bot.infinity_polling(skip_pending=True)
