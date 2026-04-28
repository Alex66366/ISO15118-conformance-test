#!/usr/bin/env python3
"""
Extract Figures (as PNG images) and Tables (as markdown files)
from the OCR-generated markdown of ISO 15118-2.

Figures: saved to ./image/figure_N.png
Tables:  saved to ./table/table_N.md
"""

import re
import os
import requests
import time

MD_FILE = "/data/cdy/code/v2g2_082/iso15118/agent/document/MinerU_markdown_ISO_15118-2-2014_2034243645514502144.md"
IMAGE_DIR = "/data/cdy/code/v2g2_082/iso15118/agent/document/image"
TABLE_DIR = "/data/cdy/code/v2g2_082/iso15118/agent/document/table"

os.makedirs(IMAGE_DIR, exist_ok=True)
os.makedirs(TABLE_DIR, exist_ok=True)

with open(MD_FILE, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Strip trailing newline for easier matching
lines = [line.rstrip("\n") for line in lines]

# ============================================================
# 1. Extract Figures
# ============================================================
# Strategy: find lines matching "Figure N — ..." (with optional # prefix).
# Then look BACKWARDS up to 15 lines for the closest image link(s).
# Some figures have multiple image parts (e.g., Figure 9 has 2 images).
# We download all images associated with a figure; if multiple, we just
# use the LAST one closest to the caption (usually the main diagram).
# Actually, for figures with multiple images, the one directly above is the main one.

figure_pattern = re.compile(r'^#*\s*Figure\s+(\d+)\s*[—–-]\s*(.+)$')
image_pattern = re.compile(r'!\[image\]\((https?://[^\)]+)\)')

figure_count = 0
figure_errors = []

for i, line in enumerate(lines):
    m = figure_pattern.match(line.strip())
    if not m:
        continue

    fig_num = int(m.group(1))
    fig_title = m.group(2).strip()

    # Search backwards up to 15 lines for image links
    # Collect ALL image URLs found, take the closest one (last found going backwards = first above)
    image_urls = []
    for j in range(i - 1, max(i - 16, -1), -1):
        img_match = image_pattern.search(lines[j])
        if img_match:
            image_urls.append(img_match.group(1))

    if not image_urls:
        print(f"  WARNING: No image found for Figure {fig_num} — {fig_title} (line {i+1})")
        figure_errors.append(fig_num)
        continue

    # The closest image to the caption is the first one found (since we search backwards)
    # For multi-image figures, we just save the closest one
    url = image_urls[0]

    # Download the image
    out_path = os.path.join(IMAGE_DIR, f"figure_{fig_num}.png")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(resp.content)
        figure_count += 1
        print(f"  OK: Figure {fig_num} — {fig_title} -> {out_path}")
    except Exception as e:
        print(f"  ERROR: Figure {fig_num} — {fig_title}: {e}")
        figure_errors.append(fig_num)

    # Be polite to the CDN
    time.sleep(0.1)

print(f"\n=== Figures: {figure_count} saved, {len(figure_errors)} errors ===\n")

# ============================================================
# 2. Extract Tables
# ============================================================
# Strategy: find lines matching "Table N — ..." (at the start of line).
# Then look FORWARDS for the first <table>...</table> block within 10 lines.
# Some tables may span multiple lines (the <table> tag content might wrap).
# Save as markdown with the title + the HTML table content.

table_pattern = re.compile(r'^Table\s+(\d+)\s*[—–-]\s*(.+)$')
table_html_start = re.compile(r'<table>')
table_html_end = re.compile(r'</table>')

table_count = 0
table_errors = []

for i, line in enumerate(lines):
    m = table_pattern.match(line.strip())
    if not m:
        continue

    tbl_num = int(m.group(1))
    tbl_title_line = line.strip()

    # Search forwards up to 10 lines for a <table> tag
    table_content = None
    for j in range(i + 1, min(i + 11, len(lines))):
        if '<table>' in lines[j]:
            # Found the start of table. Now collect until </table>
            # The table might span multiple lines
            table_lines = []
            for k in range(j, min(j + 100, len(lines))):
                table_lines.append(lines[k])
                if '</table>' in lines[k]:
                    break
            table_content = "\n".join(table_lines)
            break

    if not table_content:
        print(f"  WARNING: No <table> found for Table {tbl_num} (line {i+1})")
        table_errors.append(tbl_num)
        continue

    # Write the markdown file
    out_path = os.path.join(TABLE_DIR, f"table_{tbl_num}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"{tbl_title_line}\n\n\n{table_content}\n")

    table_count += 1
    print(f"  OK: Table {tbl_num} -> {out_path}")

print(f"\n=== Tables: {table_count} saved, {len(table_errors)} errors ===")
