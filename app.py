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
from datetime import datetime
from pathlib import Path
from functools import wraps
from urllib.parse import quote_plus

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


def collect_images(keyword, step_names):
    """为封面和每个步骤搜索对应的图片"""
    result = {"cover": [], "steps": {}}

    # 封面图：搜索成品图
    cover_queries = [
        f"{keyword} 成品 编织",
        f"{keyword} knitting finished",
        f"{keyword} 手工编织 毛衣",
    ]
    for q in cover_queries:
        result["cover"].extend(search_images_bing(q, count=6))
        if len(result["cover"]) >= 5:
            break
    if len(result["cover"]) < 3:
        for q in cover_queries[:2]:
            result["cover"].extend(search_images_google(q, count=5))

    # 去重
    result["cover"] = list(dict.fromkeys(result["cover"]))

    # 每个步骤搜索对应图片
    for step_name in step_names:
        step_queries = [
            f"{keyword} {step_name} 编织 图解",
            f"{keyword} {step_name} knitting tutorial",
        ]
        step_imgs = []
        for q in step_queries:
            step_imgs.extend(search_images_bing(q, count=4))
            if len(step_imgs) >= 3:
                break
        result["steps"][step_name] = list(dict.fromkeys(step_imgs))

    return result


def verify_image(url, timeout=5):
    """验证图片链接是否可访问"""
    try:
        resp = requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        ct = resp.headers.get("content-type", "")
        return resp.status_code == 200 and ("image" in ct or url.endswith(('.jpg', '.jpeg', '.png', '.webp')))
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# 网页搜索
# ═══════════════════════════════════════════════════════════════════════════

def search_web(keyword):
    """搜索并收集文字内容"""
    queries = [
        f"{keyword} 编织方法 教程 详细步骤",
        f"{keyword} 织法 图解 针数",
        f"{keyword} knitting pattern instructions",
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

def ai_generate(keyword, raw_content, api_key):
    """DeepSeek 生成结构化织造数据"""
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    prompt = f"""你是一位经验丰富的编织专家和手工艺教师。用户想学习「{keyword}」的编织/织造方法。

以下是从网上搜集到的资料：
<资料>
{raw_content if raw_content else "（未搜集到资料，请使用你的知识）"}
</资料>

请生成 3 种不同的「{keyword}」款式/方案（难度、风格、织法要有明显区别）。

**重要**：每个步骤的 phase 名称要具体且独特（如"起针与底边罗纹"而不只是"准备"），因为我会根据 phase 名称去搜索对应的图片。

请严格按以下 JSON 格式输出（不要输出其他内容）：

{{
  "cards": [
    {{
      "title": "款式名称",
      "subtitle": "一句话描述这个款式的特点",
      "difficulty": "入门/初级/中级/高级",
      "estimated_time": "预估时间",
      "materials": [
        {{"name": "材料", "spec": "规格", "quantity": "用量"}}
      ],
      "tools": ["工具1", "工具2"],
      "gauge": "编织密度",
      "abbreviations": [
        {{"abbr": "缩写", "full": "说明"}}
      ],
      "steps": [
        {{
          "phase": "具体阶段名（如：起针与底边罗纹编织）",
          "instructions": ["详细步骤1（包含针数行数）", "步骤2"],
          "tips": "该阶段技巧"
        }}
      ],
      "finishing": ["收尾1", "收尾2"],
      "tips": ["技巧1", "技巧2"],
      "variations": ["变化1"]
    }}
  ]
}}"""

    response = client.chat.completions.create(
        model="deepseek-chat",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.choices[0].message.content.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        fix = client.chat.completions.create(
            model="deepseek-chat",
            max_tokens=8000,
            messages=[{"role": "user", "content": f"修复此JSON，只输出合法JSON：\n{text}"}],
        )
        fix_text = fix.choices[0].message.content.strip()
        if "```json" in fix_text:
            fix_text = fix_text.split("```json")[1].split("```")[0].strip()
        elif "```" in fix_text:
            fix_text = fix_text.split("```")[1].split("```")[0].strip()
        data = json.loads(fix_text)

    return data.get("cards", [])


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
        # 1. 搜索文字
        raw_content = search_web(keyword)

        # 2. AI 生成
        cards = ai_generate(keyword, raw_content, api_key)

        # 3. 为每张卡片搜索图片
        for i, card in enumerate(cards):
            step_names = [s["phase"] for s in card.get("steps", [])]
            images = collect_images(keyword + " " + card["title"], step_names)

            # 封面图
            card["image"] = ""
            for img_url in images["cover"]:
                if verify_image(img_url):
                    card["image"] = img_url
                    break

            # 步骤图
            for step in card.get("steps", []):
                step["image"] = ""
                step_imgs = images["steps"].get(step["phase"], [])
                # 如果步骤专属图没有，用封面剩余图
                all_candidates = step_imgs + [u for u in images["cover"] if u != card["image"]]
                for img_url in all_candidates:
                    if verify_image(img_url):
                        step["image"] = img_url
                        break

            # 生成 ID
            card["id"] = hashlib.md5(
                f"{keyword}-{card['title']}-{i}-{time.time()}".encode()
            ).hexdigest()[:12]

        return jsonify({"cards": cards, "keyword": keyword})

    except Exception as e:
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
