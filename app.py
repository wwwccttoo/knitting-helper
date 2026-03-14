#!/usr/bin/env python3
"""
毛衣织造助手 - Web App
用户登录 + 搜索编织方法 + AI生成带图片卡片 + 卡片持久化存储
"""
from __future__ import annotations

import json
import os
import re
import hashlib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from functools import wraps

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, g
from openai import OpenAI

app = Flask(__name__)
app.secret_key = "knitting-helper-webapp-2024-secret"

DB_PATH = Path(__file__).parent / "knitting.db"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ═══════════════════════════════════════════════════════════════════════════
# 数据库
# ═══════════════════════════════════════════════════════════════════════════

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = sqlite3.connect(str(DB_PATH))
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            api_key TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS cards (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            title TEXT NOT NULL,
            data JSON NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    db.close()


def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


# ═══════════════════════════════════════════════════════════════════════════
# 登录装饰器
# ═══════════════════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════════════════════════════
# 图片搜索（多来源，大量抓取）
# ═══════════════════════════════════════════════════════════════════════════

def search_images_bing(query, count=15):
    """Bing 图片搜索"""
    imgs = []
    try:
        resp = requests.get(
            "https://www.bing.com/images/search",
            params={"q": query, "first": "1", "count": str(count), "qft": "+filterui:photo-photo"},
            headers=HEADERS, timeout=15,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.select("a.iusc"):
            m = a.get("m")
            if m:
                try:
                    data = json.loads(m)
                    if "murl" in data:
                        imgs.append(data["murl"])
                except (json.JSONDecodeError, KeyError):
                    continue
        if not imgs:
            for img in soup.select("img.mimg"):
                src = img.get("src") or img.get("data-src")
                if src and src.startswith("http"):
                    imgs.append(src)
    except Exception:
        pass
    return imgs[:count]


def search_images_google(query, count=10):
    """Google 图片搜索 (备用)"""
    imgs = []
    try:
        resp = requests.get(
            "https://www.google.com/search",
            params={"q": query, "tbm": "isch", "num": str(count)},
            headers=HEADERS, timeout=15,
        )
        resp.raise_for_status()
        # 提取图片链接
        for match in re.findall(r'\["(https?://[^"]+\.(?:jpg|jpeg|png|webp))"', resp.text):
            if "gstatic" not in match and "google" not in match:
                imgs.append(match)
    except Exception:
        pass
    return imgs[:count]


def collect_images(keyword):
    """一次搜索，收集足够多图片分配给封面和步骤"""
    all_imgs = []
    for q in [f"{keyword} 编织 成品 教程", f"{keyword} knitting pattern"]:
        all_imgs.extend(search_images_bing(q, count=12))
        if len(all_imgs) >= 10:
            break
    if len(all_imgs) < 5:
        all_imgs.extend(search_images_google(f"{keyword} knitting", count=8))
    # 去重
    return list(dict.fromkeys(all_imgs))


# ═══════════════════════════════════════════════════════════════════════════
# 网页搜索
# ═══════════════════════════════════════════════════════════════════════════

def search_web(keyword):
    """搜索并收集文字内容"""
    queries = [
        f"{keyword} 编织方法 教程 步骤",
        f"{keyword} knitting pattern tutorial",
    ]
    all_results = []
    for q in queries:
        try:
            resp = requests.get(
                "https://www.bing.com/search",
                params={"q": q, "count": "5"},
                headers=HEADERS, timeout=15,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for li in soup.select("li.b_algo"):
                t = li.select_one("h2 a")
                s = li.select_one(".b_caption p")
                if t:
                    all_results.append({
                        "title": t.get_text(strip=True),
                        "url": t.get("href", ""),
                        "snippet": s.get_text(strip=True) if s else "",
                    })
        except Exception:
            pass

    seen = set()
    unique = []
    for r in all_results:
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)

    collected = []
    for r in unique[:4]:
        try:
            resp = requests.get(r["url"], headers=HEADERS, timeout=10)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            content = "\n".join(lines)[:2500]
            if content:
                collected.append(f"--- {r['title']} ---\n{content}")
        except Exception:
            pass

    return "\n\n".join(collected)


# ═══════════════════════════════════════════════════════════════════════════
# AI 分析
# ═══════════════════════════════════════════════════════════════════════════

def _generate_one_card(keyword, raw_content, api_key, style_hint):
    """生成单张卡片（用于并行调用）"""
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    prompt = f"""你是编织专家。请为「{keyword}」生成一份{style_hint}的编织方案。
参考资料：{raw_content[:1500] if raw_content else "用你的知识"}

直接输出JSON，不要其他文字：
{{"title":"款式名","subtitle":"一句话描述","difficulty":"入门/初级/中级/高级","estimated_time":"时间","materials":[{{"name":"","spec":"","quantity":""}}],"tools":[""],"gauge":"密度","steps":[{{"phase":"阶段名","instructions":["步骤"],"tips":"技巧"}}],"finishing":["收尾"],"tips":["技巧"],"variations":["变化"]}}

要求：steps 限 3-4 个阶段，每阶段 instructions 限 2-3 条，每条简洁但包含关键针数。"""

    response = client.chat.completions.create(
        model="deepseek-chat",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.choices[0].message.content.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    return json.loads(text)


def ai_generate(keyword, raw_content, api_key):
    """并行生成 3 张卡片"""
    styles = ["简单入门级", "经典中级", "进阶花样级"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(_generate_one_card, keyword, raw_content, api_key, s)
            for s in styles
        ]
        cards = []
        for f in futures:
            try:
                cards.append(f.result(timeout=60))
            except Exception:
                pass

    return cards


# ═══════════════════════════════════════════════════════════════════════════
# 路由：认证
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", mode="login")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    if not username or not password:
        return render_template("login.html", mode="login", error="请填写用户名和密码")

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user or user["password_hash"] != hash_pw(password):
        return render_template("login.html", mode="login", error="用户名或密码错误")

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    return redirect(url_for("index"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("login.html", mode="register")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    if not username or not password:
        return render_template("login.html", mode="register", error="请填写用户名和密码")
    if len(password) < 4:
        return render_template("login.html", mode="register", error="密码至少4位")

    db = get_db()
    try:
        db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                   (username, hash_pw(password)))
        db.commit()
    except sqlite3.IntegrityError:
        return render_template("login.html", mode="register", error="用户名已存在")

    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ═══════════════════════════════════════════════════════════════════════════
# 路由：主页面
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/")
@login_required
def index():
    db = get_db()
    saved = db.execute(
        "SELECT id, keyword, title, data, created_at FROM cards WHERE user_id = ? ORDER BY created_at DESC",
        (session["user_id"],)
    ).fetchall()
    saved_cards = []
    for row in saved:
        d = json.loads(row["data"])
        saved_cards.append({
            "id": row["id"],
            "keyword": row["keyword"],
            "title": row["title"],
            "image": d.get("image", ""),
            "difficulty": d.get("difficulty", ""),
            "estimated_time": d.get("estimated_time", ""),
            "steps_count": len(d.get("steps", [])),
            "created_at": row["created_at"],
        })
    return render_template("index.html", username=session["username"], saved_cards=saved_cards)


@app.route("/search", methods=["POST"])
@login_required
def search():
    keyword = request.form.get("keyword", "").strip()
    api_key = request.form.get("api_key", "").strip()
    if not keyword:
        return jsonify({"error": "请输入搜索关键词"}), 400
    if not api_key:
        return jsonify({"error": "请输入 API Key"}), 400

    # 保存 api_key 到用户记录
    db = get_db()
    db.execute("UPDATE users SET api_key = ? WHERE id = ?", (api_key, session["user_id"]))
    db.commit()

    try:
        # 1. 并行：搜索文字 + 搜索图片
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_text = pool.submit(search_web, keyword)
            fut_imgs = pool.submit(collect_images, keyword)
            raw_content = fut_text.result(timeout=20)
            images = fut_imgs.result(timeout=20)

        # 2. 并行生成 3 张卡片（内部已并行）
        cards = ai_generate(keyword, raw_content, api_key)

        for i, card in enumerate(cards):
            # 封面图：每张卡片分配不同图片
            card["image"] = images[i] if i < len(images) else ""

            # 步骤图：轮流分配剩余图片
            offset = len(cards)
            for j, step in enumerate(card.get("steps", [])):
                img_idx = offset + i * 6 + j
                step["image"] = images[img_idx % len(images)] if images else ""

            card["id"] = hashlib.md5(
                f"{keyword}-{card['title']}-{i}-{time.time()}".encode()
            ).hexdigest()[:12]

        return jsonify({"cards": cards, "keyword": keyword})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"生成失败: {str(e)}"}), 500


@app.route("/card/<card_id>")
@login_required
def card_detail(card_id):
    # 先查数据库
    db = get_db()
    row = db.execute("SELECT data FROM cards WHERE id = ? AND user_id = ?",
                     (card_id, session["user_id"])).fetchone()
    if row:
        card = json.loads(row["data"])
        card["id"] = card_id
        card["saved"] = True
    else:
        # 查内存缓存（刚生成还没保存的）
        card = TEMP_CARDS.get(card_id)
        if not card:
            return "卡片未找到", 404
        card["saved"] = False
    return render_template("detail.html", card=card, username=session["username"])


# 临时缓存（未保存的卡片）
TEMP_CARDS = {}


@app.route("/save_card", methods=["POST"])
@login_required
def save_card():
    """保存卡片到数据库"""
    data = request.get_json()
    card_id = data.get("card_id")
    card_data = data.get("card_data")
    keyword = data.get("keyword", "")

    if not card_id or not card_data:
        return jsonify({"error": "数据不完整"}), 400

    db = get_db()
    # 检查是否已保存
    existing = db.execute("SELECT id FROM cards WHERE id = ? AND user_id = ?",
                          (card_id, session["user_id"])).fetchone()
    if existing:
        return jsonify({"message": "已保存", "saved": True})

    db.execute(
        "INSERT INTO cards (id, user_id, keyword, title, data) VALUES (?, ?, ?, ?, ?)",
        (card_id, session["user_id"], keyword, card_data.get("title", ""), json.dumps(card_data, ensure_ascii=False))
    )
    db.commit()

    return jsonify({"message": "保存成功", "saved": True})


@app.route("/delete_card", methods=["POST"])
@login_required
def delete_card():
    """删除已保存的卡片"""
    data = request.get_json()
    card_id = data.get("card_id")
    db = get_db()
    db.execute("DELETE FROM cards WHERE id = ? AND user_id = ?", (card_id, session["user_id"]))
    db.commit()
    return jsonify({"message": "已删除"})


@app.route("/api/store_temp", methods=["POST"])
@login_required
def store_temp():
    """前端生成卡片后临时存储到内存"""
    data = request.get_json()
    cards = data.get("cards", [])
    for card in cards:
        TEMP_CARDS[card["id"]] = card
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_ENV") != "production"
    print(f"\n🧶 毛衣织造助手 Web App")
    print(f"   打开浏览器访问: http://127.0.0.1:{port}\n")
    app.run(debug=debug, host="0.0.0.0", port=port)
