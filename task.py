#!/usr/bin/env python3

import os
import re
import sys
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import pandas as pd
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from gigachat import GigaChat


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_B2B")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2")
GIGACHAT_VERIFY_SSL = os.getenv("GIGACHAT_VERIFY_SSL", "false").lower() == "true"

if not GIGACHAT_CREDENTIALS:
    raise RuntimeError("GIGACHAT_CREDENTIALS is not set in .env")

giga = GigaChat(
    credentials=GIGACHAT_CREDENTIALS,
    scope=GIGACHAT_SCOPE,
    model=GIGACHAT_MODEL,
    verify_ssl_certs=GIGACHAT_VERIFY_SSL,
    timeout=60,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 site-text-audit-bot"
}


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_url(url: str) -> str:
    return url.split("#")[0].rstrip("/")


def same_domain(url: str, root_domain: str) -> bool:
    return urlparse(url).netloc == root_domain


def is_useful_text(text: str) -> bool:
    if len(text) < 40:
        return False

    if " " not in text:
        return False

    bad_fragments = [
        "cookie",
        "javascript",
        "whatsapp",
        "telegram",
        "личный кабинет",
        "войти",
        "регистрация",
    ]

    low = text.lower()
    if any(x in low for x in bad_fragments):
        return False

    return True


def extract_text_and_links(url: str):
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()

    text_blocks = []
    seen = set()
    block_id = 1

    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "span", "div"]):
        value = clean_text(el.get_text(" "))

        if not is_useful_text(value):
            continue

        if value in seen:
            continue

        seen.add(value)

        text_blocks.append({
            "block_id": block_id,
            "tag": el.name,
            "text": value
        })

        block_id += 1

    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()

        if not href:
            continue

        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue

        links.append(urljoin(url, href))

    return text_blocks, links


def crawl(start_url: str, max_pages: int):
    root_domain = urlparse(start_url).netloc
    queue = [normalize_url(start_url)]
    visited = set()
    pages = []

    while queue and len(visited) < max_pages:
        url = normalize_url(queue.pop(0))

        if url in visited:
            continue

        if not same_domain(url, root_domain):
            continue

        try:
            text_blocks, links = extract_text_and_links(url)

            pages.append({
                "url": url,
                "text_blocks": text_blocks,
                "technical_error": ""
            })

            visited.add(url)

            for link in links:
                link = normalize_url(link)
                if link not in visited and same_domain(link, root_domain):
                    queue.append(link)

            time.sleep(0.5)

        except Exception as e:
            pages.append({
                "url": url,
                "text_blocks": [],
                "technical_error": str(e)
            })
            visited.add(url)

    return pages


def make_chunks(text_blocks, max_chars=4500):
    chunks = []
    current = []
    current_len = 0

    for block in text_blocks:
        line = f'BLOCK_ID={block["block_id"]}; TAG={block["tag"]}; TEXT="{block["text"]}"'
        line_len = len(line)

        if current and current_len + line_len > max_chars:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len

    if current:
        chunks.append("\n".join(current))

    return chunks


def extract_json_array(text: str):
    cleaned = text.strip()
    cleaned = cleaned.replace("```json", "").replace("```", "").strip()

    start = cleaned.find("[")
    end = cleaned.rfind("]")

    if start == -1 or end == -1 or end <= start:
        raise ValueError("JSON array not found")

    return json.loads(cleaned[start:end + 1])


def check_chunk(url: str, chunk: str):
    prompt = f"""
Ты профессиональный корректор и редактор русскоязычных сайтов.

Нужно найти ТОЛЬКО реальные ошибки:
- орфографические
- грамматические
- пунктуационные
- логические
- стилистические

Не считай ошибками:
- короткие элементы меню
- названия категорий
- отдельные слова
- кнопки
- SEO-фразы без явной ошибки
- отсутствие контекста

Каждая проверяемая строка имеет формат:
BLOCK_ID=номер; TAG=html_тег; TEXT="текст"

Верни СТРОГО JSON-массив.
Без markdown, без пояснений, без текста до или после JSON.

Формат:
[
  {{
    "block_id": 1,
    "error_type": "орфография|грамматика|пунктуация|логика|стиль|другое",
    "fragment": "точная цитата с ошибкой",
    "problem": "объяснение, в чём ошибка",
    "suggestion": "исправленный вариант"
  }}
]

Если ошибок нет, верни:
[]

URL страницы:
{url}

Текст:
{chunk}
"""

    response = giga.chat(prompt)
    content = response.choices[0].message.content.strip()
    return extract_json_array(content)


def run_audit(start_url: str, max_pages: int):
    pages = crawl(start_url, max_pages)
    rows = []

    for page in pages:
        url = page["url"]

        if page["technical_error"]:
            rows.append({
                "url": url,
                "block_id": "",
                "html_tag": "",
                "fragment": "",
                "error_type": "technical",
                "problem": page["technical_error"],
                "suggestion": ""
            })
            continue

        block_map = {
            block["block_id"]: block
            for block in page["text_blocks"]
        }

        chunks = make_chunks(page["text_blocks"])

        for chunk in chunks:
            try:
                issues = check_chunk(url, chunk)
            except Exception as e:
                rows.append({
                    "url": url,
                    "block_id": "",
                    "html_tag": "",
                    "fragment": "",
                    "error_type": "technical",
                    "problem": f"Ошибка проверки через GigaChat: {e}",
                    "suggestion": ""
                })
                continue

            for issue in issues:
                block_id = issue.get("block_id")
                block = block_map.get(block_id, {})

                rows.append({
                    "url": url,
                    "block_id": block_id,
                    "html_tag": block.get("tag", ""),
                    "fragment": issue.get("fragment", ""),
                    "error_type": issue.get("error_type", ""),
                    "problem": issue.get("problem", ""),
                    "suggestion": issue.get("suggestion", "")
                })

    return rows, pages


def save_report(start_url: str, rows: list, pages: list):
    domain = urlparse(start_url).netloc.replace(".", "_")
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    xlsx_path = BASE_DIR / f"site_audit_{domain}_{timestamp}.xlsx"

    if rows:
        errors_df = pd.DataFrame(rows)
    else:
        errors_df = pd.DataFrame([{
            "url": start_url,
            "block_id": "",
            "html_tag": "",
            "fragment": "",
            "error_type": "none",
            "problem": "Ошибки не найдены",
            "suggestion": ""
        }])

    pages_df = pd.DataFrame([
        {
            "url": page["url"],
            "text_blocks_found": len(page["text_blocks"]),
            "technical_error": page["technical_error"]
        }
        for page in pages
    ])

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        errors_df.to_excel(writer, sheet_name="Ошибки", index=False)

    return xlsx_path


def main():
    try:
        start_url = sys.argv[1]
        max_pages = int(sys.argv[2])

        rows, pages = run_audit(start_url, max_pages)
        xlsx_path = save_report(start_url, rows, pages)

        print(json.dumps({
            "ok": True,
            "site": start_url,
            "pages_checked": len(pages),
            "issues_found": len(rows),
            "xlsx": str(xlsx_path)
        }, ensure_ascii=False, indent=2))

    except Exception as e:
        print(json.dumps({
            "ok": False,
            "error": str(e)
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
