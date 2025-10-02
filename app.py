import json
import random
import os
import time
from pathlib import Path
from datetime import datetime

from flask import Flask, render_template, request, jsonify, session
from openai import OpenAI
from dotenv import load_dotenv

# Markdown + sanitization
from markdown import markdown
import bleach
from markupsafe import Markup

# --- Config ---
JSON_FILE = "flashcards.json"       # Your prepared Q&A JSON
OUTPUT_DIR = Path("runs")
OUTPUT_DIR.mkdir(exist_ok=True)

# Initialize Flask + secret for session
load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "supersecretkey")

# Initialize client with key from .env
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Allowed HTML tags/attributes for bleach sanitization
ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS).union({
    "p", "pre", "code", "span", "h1", "h2", "h3",
    "table", "thead", "tbody", "tr", "th", "td",
    "em", "strong", "ul", "ol", "li", "br"
})

ALLOWED_ATTRIBUTES = {
    **bleach.sanitizer.ALLOWED_ATTRIBUTES,
    "code": ["class"],
    "span": ["class"],
    "a": ["href", "title", "rel"],
    "img": ["src", "alt", "title"],
}

def render_markdown(md_text: str) -> Markup:
    """Convert Markdown to HTML and sanitize the result. Returns Markup (safe) for templates."""
    if not md_text:
        return Markup("")
    # Convert Markdown -> HTML
    html = markdown(md_text, extensions=["fenced_code", "tables", "nl2br", "sane_lists"])
    # Sanitize HTML but keep UTF-8 intact
    clean = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES, strip=True)
    return Markup(clean)


def load_flashcards():
    """Load flashcards from JSON, preserving UTF-8 characters."""
    with open(JSON_FILE, encoding="utf-8") as f:
        rows = json.load(f)
    # No decoding / re-encoding needed — JSON with UTF-8 is already correct
    return rows


flashcards = load_flashcards()

@app.before_request
def setup_session():
    if "order" not in session:
        order = list(range(len(flashcards)))
        random.shuffle(order)
        session["order"] = order
        session["index"] = 0
        session["logfile"] = str(OUTPUT_DIR / f"session_{int(time.time())}.json")
        # Initialize JSON log as empty array
        with open(session["logfile"], "w", encoding="utf-8") as f:
            json.dump([], f)

@app.route("/")
def index():
    idx = session["order"][session["index"]]
    card = flashcards[idx].copy()
    card_answer_html = render_markdown(card.get("answer", ""))
    return render_template(
        "index.html",
        card=card,
        card_answer_html=card_answer_html,
        pos=session["index"],
        total=len(flashcards),
    )

@app.route("/submit", methods=["POST"])
def submit():
    data = request.json
    idx = session["order"][session["index"]]
    card = flashcards[idx]
    user_answer = data.get("answer", "")

    # --- Query OpenAI for feedback ---
    try:
        response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert senior C++ engineer and interviewer. "
                    "Given a C++ question and a user's answer, do the following:\n"
                    "1. Evaluate the user's answer and give constructive feedback.\n"
                    "2. Provide the correct answer or explanation, only if the user's answer was incorrect.\n"
                    "3. Format your response clearly with Markdown (bold for emphasis, code blocks for examples).\n"
                    "4. Be detailed but concise, suitable for an engineer learning C++ deeply."
                )
            },
            {
                "role": "user",
                "content": (
                    f"Question: {card['question']}\n"
                    f"User's answer: {user_answer or '(No answer provided)'}"
                )
            }
        ],
        max_completion_tokens=350, 
        temperature=0.2  # low randomness for more precise answers
    )
        feedback_text = response.choices[0].message.content.strip()

        # print("OpenAI feedback:", feedback_text)
    except Exception as e:
        feedback_text = f"Error contacting OpenAI: {e}"

    # Convert feedback into safe HTML for frontend
    feedback_html = str(render_markdown(feedback_text))

    # --- Append to JSON log ---
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "index": idx,
        "question": card["question"],
        "user_answer": user_answer,
        "openai_feedback": feedback_text
    }

    logfile = Path(session["logfile"])
    if not logfile.exists():
        logfile.write_text("[]", encoding="utf-8")  # Initialize as empty JSON array

    # Read existing log
    try:
        with open(logfile, "r", encoding="utf-8") as f:
            log = json.load(f)
    except json.JSONDecodeError:
        log = []

    # Append new entry
    log.append(log_entry)

    # Save back to file
    with open(logfile, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)

    return jsonify({"feedback_html": feedback_html})



@app.route("/navigate", methods=["POST"])
def navigate():
    action = request.json.get("action")
    if action == "next":
        session["index"] = min(session["index"] + 1, len(flashcards) - 1)
    elif action == "previous":
        session["index"] = max(session["index"] - 1, 0)
    elif action == "skip":
        session["index"] = min(session["index"] + 1, len(flashcards) - 1)

    idx = session["order"][session["index"]]
    card = flashcards[idx].copy()
    answer_html = str(render_markdown(card.get("answer", "")))
    return jsonify({
        "index": idx,
        "question": card.get("question", ""),
        "answer_html": answer_html,
        "pos": session["index"],
        "total": len(flashcards)
    })

@app.route("/view")
def view():
    logfile = session.get("logfile")
    if not logfile or not Path(logfile).exists():
        return "No session log found."
    with open(logfile, encoding="utf-8") as f:
        rows = json.load(f)

    for r in rows:
        r["openai_feedback_html"] = render_markdown(r.get("openai_feedback", ""))
    return render_template("view.html", rows=rows)

if __name__ == "__main__":
    app.run(debug=True)

