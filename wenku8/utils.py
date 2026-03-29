import asyncio
import functools
import time

from lxml.html import Element

from wenku8.models import NovelIndex


def extract_text(parser: Element, xpath: str, split: bool = False) -> str:
    if split:
        return separate_chinese_colon(parser.xpath(xpath)[0].text)[1]
    else:
        return parser.xpath(xpath)[0].text


def separate_chinese_colon(text: str):
    if "︰" in text:
        return text.split("︰")
    else:
        return text.split("：")


def cooldown(seconds):
    def decorator(func):
        last_finished_time = 0
        lock = asyncio.Lock()

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            nonlocal last_finished_time

            # 1. 获取锁，确保一次只能有一个请求在进行时间检查或执行
            async with lock:
                # 2. 计算需要等待的时间
                current_time = time.monotonic()
                elapsed = current_time - last_finished_time
                wait_time = seconds - elapsed

                # 3. 如果冷却未好，则异步睡眠（不阻塞主线程）
                if wait_time > 0:
                    await asyncio.sleep(wait_time)

                try:
                    # 4. 执行原函数
                    return await func(*args, **kwargs)
                finally:
                    last_finished_time = time.monotonic()

        return wrapper

    return decorator


def get_chapter_content(full_text: str, novel_index: NovelIndex, target_cid: int) -> str:
    all_headers = []
    for vol in novel_index.volumes:
        for chap in vol.chapters:
            all_headers.append((chap.cid, f"{vol.title} {chap.title}"))
            
    target_header = None
    next_header = None
    
    for i, (cid, header) in enumerate(all_headers):
        if cid == target_cid:
            target_header = header
            if i + 1 < len(all_headers):
                next_header = all_headers[i + 1][1]
            break
            
    if not target_header:
        return ""

    start_idx = full_text.find(target_header)
    if start_idx == -1:
        return ""
        
    start_idx += len(target_header)

    if next_header:
        end_idx = full_text.find(next_header, start_idx)
        raw_content = full_text[start_idx:end_idx] if end_idx != -1 else full_text[start_idx:]
    else:
        raw_content = full_text[start_idx:]

    lines = raw_content.split('\n')

    for i, line in enumerate(lines):
        if line.strip():
            first_text_idx = i
            break
    else:
        first_text_idx = len(lines)

    clean_lines = lines[first_text_idx:]

    return '\n'.join(clean_lines).rstrip()
