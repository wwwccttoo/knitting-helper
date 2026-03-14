#!/usr/bin/env python3
"""
毛衣织造助手 - Knitting Pattern Helper
功能：搜索网络上的编织方法，用 AI 解析织造过程，生成制作过程卡片。
"""

from __future__ import annotations

import os
import sys
import json
import argparse
import webbrowser
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from jinja2 import Template
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

console = Console()

# ─── 搜索模块 ───────────────────────────────────────────────────────────────

SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def search_bing(query: str, count: int = 8) -> list[dict]:
    """用 Bing 搜索编织相关内容，返回 [{title, url, snippet}]"""
    url = "https://www.bing.com/search"
    params = {"q": query, "count": str(count)}
    try:
        resp = requests.get(url, params=params, headers=SEARCH_HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        console.print(f"[yellow]Bing 搜索出错: {e}[/yellow]")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for li in soup.select("li.b_algo"):
        title_el = li.select_one("h2 a")
        snippet_el = li.select_one(".b_caption p")
        if title_el:
            results.append({
                "title": title_el.get_text(strip=True),
                "url": title_el.get("href", ""),
                "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
            })
    return results


def search_baidu(query: str, count: int = 8) -> list[dict]:
    """用百度搜索编织相关内容"""
    url = "https://www.baidu.com/s"
    params = {"wd": query, "rn": str(count)}
    try:
        resp = requests.get(url, params=params, headers=SEARCH_HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        console.print(f"[yellow]百度搜索出错: {e}[/yellow]")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for div in soup.select("div.result, div.c-container"):
        title_el = div.select_one("h3 a")
        snippet_el = div.select_one(".c-abstract, .content-right_8Zs40")
        if title_el:
            results.append({
                "title": title_el.get_text(strip=True),
                "url": title_el.get("href", ""),
                "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
            })
    return results[:count]


def fetch_page_content(url: str, max_chars: int = 5000) -> str:
    """抓取网页正文内容"""
    try:
        resp = requests.get(url, headers=SEARCH_HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
    except requests.RequestException:
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    # 移除无关标签
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)
    # 清理多余空行
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)[:max_chars]


def search_and_collect(keyword: str) -> str:
    """搜索并收集与编织相关的网页内容"""
    queries = [
        f"{keyword} 编织方法 教程",
        f"{keyword} 织法 步骤 图解",
        f"{keyword} knitting pattern tutorial",
    ]

    all_results = []
    for q in queries:
        console.print(f"  [dim]搜索: {q}[/dim]")
        results = search_bing(q, count=5)
        if not results:
            results = search_baidu(q, count=5)
        all_results.extend(results)

    # 去重
    seen_urls = set()
    unique = []
    for r in all_results:
        if r["url"] not in seen_urls:
            seen_urls.add(r["url"])
            unique.append(r)

    if not unique:
        console.print("[red]未找到搜索结果，将仅使用 AI 内部知识生成。[/red]")
        return ""

    console.print(f"  [green]找到 {len(unique)} 条结果，正在抓取内容...[/green]")

    # 抓取前 5 个页面的内容
    collected = []
    for i, r in enumerate(unique[:5]):
        console.print(f"  [dim]抓取 ({i+1}/5): {r['title'][:40]}...[/dim]")
        content = fetch_page_content(r["url"], max_chars=3000)
        if content:
            collected.append(
                f"--- 来源: {r['title']} ---\n{r['snippet']}\n{content}\n"
            )

    return "\n\n".join(collected)


# ─── AI 分析模块 ────────────────────────────────────────────────────────────

def analyze_with_ai(keyword: str, raw_content: str, api_key: str) -> dict:
    """用 DeepSeek 分析搜索内容，生成结构化织造过程"""
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    prompt = f"""你是一位经验丰富的编织专家和手工艺教师。
用户想学习「{keyword}」的编织/织造方法。

以下是从网上搜集到的相关资料（可能包含中英文内容）：

<搜集资料>
{raw_content if raw_content else "（未搜集到网络资料，请使用你的知识）"}
</搜集资料>

请综合分析以上资料（如果有的话）和你自身的编织知识，生成一份详细的「{keyword}」制作指南。

请严格按以下 JSON 格式输出（不要输出其他内容）：

{{
  "title": "作品名称",
  "difficulty": "难度等级（入门/初级/中级/高级）",
  "estimated_time": "预估完成时间",
  "materials": [
    {{"name": "材料名称", "spec": "规格说明", "quantity": "用量"}}
  ],
  "tools": ["工具1", "工具2"],
  "gauge": "密度/针数参考（如：10cm x 10cm = 20针 x 28行）",
  "abbreviations": [
    {{"abbr": "缩写", "full": "全称说明"}}
  ],
  "steps": [
    {{
      "phase": "阶段名称（如：起针、织身片、收针等）",
      "instructions": [
        "具体步骤1（尽量详细，包含针数、行数）",
        "具体步骤2"
      ],
      "tips": "该阶段的注意事项或小技巧"
    }}
  ],
  "finishing": [
    "收尾步骤1",
    "收尾步骤2"
  ],
  "tips": [
    "通用技巧提示1",
    "通用技巧提示2"
  ],
  "variations": [
    "变化款式建议1",
    "变化款式建议2"
  ]
}}"""

    console.print("  [cyan]AI 正在分析和生成织造指南...[/cyan]")

    response = client.chat.completions.create(
        model="deepseek-chat",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = response.choices[0].message.content.strip()

    # 提取 JSON（可能被 ```json ``` 包裹）
    if "```json" in response_text:
        response_text = response_text.split("```json")[1].split("```")[0].strip()
    elif "```" in response_text:
        response_text = response_text.split("```")[1].split("```")[0].strip()

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        console.print("[red]AI 输出格式异常，尝试修复...[/red]")
        fix_resp = client.chat.completions.create(
            model="deepseek-chat",
            max_tokens=4096,
            messages=[
                {"role": "user", "content": f"请修复以下 JSON 使其合法，只输出 JSON：\n{response_text}"}
            ],
        )
        fix_text = fix_resp.choices[0].message.content.strip()
        if "```json" in fix_text:
            fix_text = fix_text.split("```json")[1].split("```")[0].strip()
        elif "```" in fix_text:
            fix_text = fix_text.split("```")[1].split("```")[0].strip()
        data = json.loads(fix_text)

    return data


# ─── 卡片生成模块 ───────────────────────────────────────────────────────────

CARD_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ data.title }} - 织造过程卡片</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;600;700&family=Noto+Sans+SC:wght@300;400;500;700&display=swap');

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: 'Noto Sans SC', sans-serif;
    background: linear-gradient(135deg, #fdf6f0 0%, #f0e6d8 50%, #e8ddd0 100%);
    min-height: 100vh;
    padding: 40px 20px;
    color: #3a3028;
  }

  .card-container {
    max-width: 900px;
    margin: 0 auto;
  }

  /* 标题卡片 */
  .header-card {
    background: linear-gradient(145deg, #d4a574, #c4956a);
    border-radius: 24px;
    padding: 48px 40px;
    text-align: center;
    color: white;
    margin-bottom: 24px;
    box-shadow: 0 12px 40px rgba(180, 140, 100, 0.3);
    position: relative;
    overflow: hidden;
  }
  .header-card::before {
    content: '🧶';
    position: absolute;
    font-size: 120px;
    opacity: 0.1;
    top: -20px;
    right: -10px;
  }
  .header-card h1 {
    font-family: 'Noto Serif SC', serif;
    font-size: 2.4em;
    margin-bottom: 12px;
    text-shadow: 0 2px 8px rgba(0,0,0,0.15);
  }
  .header-meta {
    display: flex;
    justify-content: center;
    gap: 32px;
    flex-wrap: wrap;
    margin-top: 16px;
  }
  .meta-item {
    background: rgba(255,255,255,0.2);
    border-radius: 12px;
    padding: 10px 20px;
    backdrop-filter: blur(4px);
  }
  .meta-label { font-size: 0.8em; opacity: 0.85; }
  .meta-value { font-size: 1.1em; font-weight: 600; }

  /* 通用 section 卡片 */
  .section-card {
    background: white;
    border-radius: 20px;
    padding: 32px 36px;
    margin-bottom: 20px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.06);
    border: 1px solid rgba(200, 180, 160, 0.2);
  }
  .section-card h2 {
    font-family: 'Noto Serif SC', serif;
    font-size: 1.5em;
    color: #8b6940;
    margin-bottom: 20px;
    padding-bottom: 12px;
    border-bottom: 2px solid #f0e6d8;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .section-icon {
    font-size: 1.3em;
  }

  /* 材料表格 */
  .materials-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 12px;
  }
  .material-item {
    background: #faf6f2;
    border-radius: 12px;
    padding: 16px;
    border-left: 4px solid #d4a574;
  }
  .material-name { font-weight: 600; color: #6b4f35; }
  .material-detail { font-size: 0.9em; color: #8a7a6a; margin-top: 4px; }

  /* 工具标签 */
  .tools-list {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }
  .tool-tag {
    background: linear-gradient(135deg, #f5ede5, #efe3d6);
    border-radius: 20px;
    padding: 8px 18px;
    font-size: 0.95em;
    color: #6b4f35;
    border: 1px solid #e0d0c0;
  }

  /* 缩写表 */
  .abbr-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 8px;
  }
  .abbr-item {
    display: flex;
    gap: 8px;
    align-items: baseline;
    padding: 6px 0;
  }
  .abbr-code {
    background: #8b6940;
    color: white;
    border-radius: 6px;
    padding: 2px 10px;
    font-weight: 600;
    font-size: 0.9em;
    white-space: nowrap;
  }
  .abbr-desc { color: #6b5a48; font-size: 0.9em; }

  /* 步骤卡片 */
  .step-phase {
    background: #faf6f2;
    border-radius: 16px;
    padding: 28px;
    margin-bottom: 16px;
    border: 1px solid #efe3d6;
  }
  .phase-header {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 18px;
  }
  .phase-number {
    background: linear-gradient(135deg, #d4a574, #b8895a);
    color: white;
    width: 42px;
    height: 42px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    font-size: 1.1em;
    flex-shrink: 0;
  }
  .phase-title {
    font-family: 'Noto Serif SC', serif;
    font-size: 1.2em;
    color: #6b4f35;
    font-weight: 600;
  }
  .instruction-list {
    list-style: none;
    padding-left: 56px;
  }
  .instruction-list li {
    position: relative;
    padding: 8px 0 8px 24px;
    line-height: 1.7;
    color: #4a3f34;
  }
  .instruction-list li::before {
    content: '';
    position: absolute;
    left: 0;
    top: 16px;
    width: 8px;
    height: 8px;
    background: #d4a574;
    border-radius: 50%;
  }
  .phase-tip {
    margin-top: 14px;
    margin-left: 56px;
    background: #fff8f0;
    border-left: 3px solid #e6a84d;
    border-radius: 0 8px 8px 0;
    padding: 12px 16px;
    font-size: 0.92em;
    color: #8a6b3a;
  }
  .phase-tip::before { content: '💡 '; }

  /* 技巧和变化 */
  .tips-list, .finishing-list, .variations-list {
    list-style: none;
  }
  .tips-list li, .finishing-list li, .variations-list li {
    padding: 10px 0 10px 28px;
    position: relative;
    line-height: 1.6;
    border-bottom: 1px solid #f5f0ea;
  }
  .tips-list li:last-child, .finishing-list li:last-child, .variations-list li:last-child {
    border-bottom: none;
  }
  .tips-list li::before { content: '✨'; position: absolute; left: 0; }
  .finishing-list li::before { content: '🪡'; position: absolute; left: 0; }
  .variations-list li::before { content: '🎨'; position: absolute; left: 0; }

  /* 密度信息 */
  .gauge-box {
    background: linear-gradient(135deg, #f5ede5, #efe3d6);
    border-radius: 12px;
    padding: 20px 24px;
    text-align: center;
    font-size: 1.1em;
    color: #6b4f35;
    border: 1px dashed #d4a574;
  }

  /* 页脚 */
  .footer {
    text-align: center;
    padding: 24px;
    color: #b0a090;
    font-size: 0.85em;
  }

  @media print {
    body { background: white; padding: 0; }
    .section-card { box-shadow: none; border: 1px solid #ddd; break-inside: avoid; }
    .header-card { box-shadow: none; }
  }
</style>
</head>
<body>
<div class="card-container">

  <!-- 标题 -->
  <div class="header-card">
    <h1>{{ data.title }}</h1>
    <div class="header-meta">
      <div class="meta-item">
        <div class="meta-label">难度</div>
        <div class="meta-value">{{ data.difficulty }}</div>
      </div>
      <div class="meta-item">
        <div class="meta-label">预估时间</div>
        <div class="meta-value">{{ data.estimated_time }}</div>
      </div>
    </div>
  </div>

  <!-- 密度 -->
  {% if data.gauge %}
  <div class="section-card">
    <h2><span class="section-icon">📐</span> 编织密度</h2>
    <div class="gauge-box">{{ data.gauge }}</div>
  </div>
  {% endif %}

  <!-- 材料 -->
  <div class="section-card">
    <h2><span class="section-icon">🧵</span> 所需材料</h2>
    <div class="materials-grid">
      {% for m in data.materials %}
      <div class="material-item">
        <div class="material-name">{{ m.name }}</div>
        <div class="material-detail">{{ m.spec }} · {{ m.quantity }}</div>
      </div>
      {% endfor %}
    </div>
  </div>

  <!-- 工具 -->
  <div class="section-card">
    <h2><span class="section-icon">🔧</span> 所需工具</h2>
    <div class="tools-list">
      {% for t in data.tools %}
      <span class="tool-tag">{{ t }}</span>
      {% endfor %}
    </div>
  </div>

  <!-- 缩写说明 -->
  {% if data.abbreviations %}
  <div class="section-card">
    <h2><span class="section-icon">📖</span> 术语缩写</h2>
    <div class="abbr-grid">
      {% for a in data.abbreviations %}
      <div class="abbr-item">
        <span class="abbr-code">{{ a.abbr }}</span>
        <span class="abbr-desc">{{ a.full }}</span>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  <!-- 织造步骤 -->
  <div class="section-card">
    <h2><span class="section-icon">🧶</span> 织造步骤</h2>
    {% for step in data.steps %}
    <div class="step-phase">
      <div class="phase-header">
        <div class="phase-number">{{ loop.index }}</div>
        <div class="phase-title">{{ step.phase }}</div>
      </div>
      <ul class="instruction-list">
        {% for inst in step.instructions %}
        <li>{{ inst }}</li>
        {% endfor %}
      </ul>
      {% if step.tips %}
      <div class="phase-tip">{{ step.tips }}</div>
      {% endif %}
    </div>
    {% endfor %}
  </div>

  <!-- 收尾 -->
  {% if data.finishing %}
  <div class="section-card">
    <h2><span class="section-icon">🪡</span> 收尾工作</h2>
    <ul class="finishing-list">
      {% for f in data.finishing %}
      <li>{{ f }}</li>
      {% endfor %}
    </ul>
  </div>
  {% endif %}

  <!-- 技巧提示 -->
  {% if data.tips %}
  <div class="section-card">
    <h2><span class="section-icon">✨</span> 实用技巧</h2>
    <ul class="tips-list">
      {% for tip in data.tips %}
      <li>{{ tip }}</li>
      {% endfor %}
    </ul>
  </div>
  {% endif %}

  <!-- 变化款式 -->
  {% if data.variations %}
  <div class="section-card">
    <h2><span class="section-icon">🎨</span> 款式变化</h2>
    <ul class="variations-list">
      {% for v in data.variations %}
      <li>{{ v }}</li>
      {% endfor %}
    </ul>
  </div>
  {% endif %}

  <div class="footer">
    生成时间：{{ generated_at }} &nbsp;|&nbsp; 由「毛衣织造助手」自动生成
  </div>

</div>
</body>
</html>"""


def generate_card(data: dict, output_dir: Path) -> Path:
    """生成 HTML 制作过程卡片"""
    template = Template(CARD_TEMPLATE)
    html = template.render(
        data=data,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

    # 文件名安全处理
    safe_name = "".join(c if c.isalnum() or c in "_ -" else "_" for c in data["title"])
    filename = f"{safe_name}_织造卡片.html"
    output_path = output_dir / filename

    output_path.write_text(html, encoding="utf-8")
    return output_path


# ─── 主程序 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="毛衣织造助手 - 搜索编织方法，AI 分析，生成制作过程卡片"
    )
    parser.add_argument("keyword", help="搜索关键词，如：麻花毛衣、渔夫毛衣、阿兰花样围巾")
    parser.add_argument("--api-key", help="DeepSeek API Key（也可设置 DEEPSEEK_API_KEY 环境变量）")
    parser.add_argument("--output-dir", default=".", help="卡片输出目录（默认当前目录）")
    parser.add_argument("--no-search", action="store_true", help="跳过网络搜索，仅用 AI 知识生成")
    parser.add_argument("--no-open", action="store_true", help="生成后不自动打开浏览器")

    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        # 尝试从 .env 文件读取
        env_file = Path(__file__).parent / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY=") and not line.startswith("#"):
                    api_key = line.split("=", 1)[1].strip()
                    break

    if not api_key:
        console.print("[red]错误：请提供 API Key！[/red]")
        console.print("  方法1: --api-key sk-xxxxx")
        console.print("  方法2: export DEEPSEEK_API_KEY=sk-xxxxx")
        console.print("  方法3: 复制 .env.example 为 .env 并填入 key")
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    console.print(Panel(
        f"[bold]搜索关键词:[/bold] {args.keyword}",
        title="🧶 毛衣织造助手",
        border_style="bright_yellow",
    ))

    # Step 1: 搜索
    raw_content = ""
    if not args.no_search:
        console.print("\n[bold cyan]📡 Step 1: 搜索网络资料[/bold cyan]")
        raw_content = search_and_collect(args.keyword)
    else:
        console.print("\n[dim]跳过网络搜索，使用 AI 知识生成。[/dim]")

    # Step 2: AI 分析
    console.print("\n[bold cyan]🤖 Step 2: AI 智能分析[/bold cyan]")
    data = analyze_with_ai(args.keyword, raw_content, api_key)
    console.print("  [green]分析完成！[/green]")

    # 终端预览
    console.print(f"\n  标题: [bold]{data['title']}[/bold]")
    console.print(f"  难度: {data['difficulty']}  |  预估时间: {data['estimated_time']}")
    console.print(f"  材料: {len(data.get('materials', []))} 种  |  步骤: {len(data.get('steps', []))} 个阶段")

    # Step 3: 生成卡片
    console.print("\n[bold cyan]🎨 Step 3: 生成制作过程卡片[/bold cyan]")
    card_path = generate_card(data, output_dir)
    console.print(f"  [green]卡片已生成: {card_path}[/green]")

    # 同时保存 JSON 数据
    json_path = card_path.with_suffix(".json")
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"  [dim]数据已保存: {json_path}[/dim]")

    if not args.no_open:
        webbrowser.open(f"file://{card_path.resolve()}")
        console.print("\n  [dim]已在浏览器中打开卡片。[/dim]")

    console.print("\n[bold green]✅ 完成！[/bold green]\n")


if __name__ == "__main__":
    main()
