"""Safe verification: status + DRY-RUN scan, no model call, no paper positions.

Original self-test bypassed event_text(); V2 intentionally cannot bypass the
code-level event gate. For offline tests run: py -m pytest -q tests
"""
import asyncio
import autoshadow

async def notify(text):
    print(text, flush=True)

print(autoshadow.describe_status())
asyncio.run(autoshadow.process_signals(notify, dry=True))
