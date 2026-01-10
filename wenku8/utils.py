import asyncio
import functools
import time

from lxml.html import Element


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
