"""Render the approved daily digest; no network or sending side effects."""

import argparse
from datetime import date
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


TEMPLATE = Path(__file__).parent / "templates" / "daily_digest.json"
# A visible-width blank paragraph keeps spacing in clients that collapse empty lines.
BLOCK_SEPARATOR = "\n\n\u3000\n\n"


def text(value):
    """Keep original visible text while escaping Markdown control characters."""
    if not isinstance(value, str) or not value.strip() or any(
        ord(char) < 32 for char in value
    ):
        raise ValueError("Task fields must be nonempty single-line strings")
    return re.sub(r"([\\`*_{}\[\]<>()!#+.>|~-])", r"\\\1", value)


def render_digest(data):
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    categories = template["categories"]
    keys = [category["key"] for category in categories]
    if len(keys) != len(set(keys)):
        raise ValueError("Template categories must be unique")
    day = data["date"]
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("Date must use YYYY-MM-DD")
    groups = {key: [] for key in keys}
    seen = set()
    for task in data["tasks"]:
        if task["category"] not in groups:
            raise ValueError("Unknown task category")
        url = task["url"]
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.username or parsed.password
                or any(char.isspace() or char in "<>\\" for char in url)):
            raise ValueError("Expected a direct HTTP(S) task URL")
        if url in seen:
            raise ValueError("A task may appear only once, in one category")
        seen.add(url)
        groups[task["category"]].append(task)

    active = [category for category in categories if groups[category["key"]]]
    counts = "、".join(
        f'{category["label"]} {len(groups[category["key"]])} 项'
        for category in active
    )
    overview = f'更新 {len(data["tasks"])} 项' + (f"：{counts}" if counts else "")
    blocks = [f'**{template["title"]}**  \n日期：{day}  \n{overview}']
    for category in active:
        tasks = groups[category["key"]]
        heading = " ".join(filter(None, [category["icon"], category["label"]]))
        task_blocks = []
        for index, task in enumerate(tasks, 1):
            # Literal numbering prevents ordered-list indentation in Markdown clients.
            prefix = f"{index}\\. "
            task_blocks.append(
                f'{prefix}[{text(task["title"])}](<{task["url"]}>)  \n'
                f'{text(task["summary"])}  \n'
                f'当前：{text(task["status"])}｜负责人：{text(task["owner"])}'
            )
        blocks.append(f"**{heading}（{len(tasks)}）**  \n" + BLOCK_SEPARATOR.join(task_blocks))
    return BLOCK_SEPARATOR.join(blocks) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    print(render_digest(json.loads(args.input.read_text(encoding="utf-8"))), end="")


if __name__ == "__main__":
    main()
