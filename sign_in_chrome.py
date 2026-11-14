"""Interactive sign-in helper, independent of an ongoing older worker run."""
import asyncio
from pathlib import Path
import os
from onq.browser import Browsers

class Profiles:
    state=Path(os.environ['LOCALAPPDATA'])/'onQ Library'

async def main():
    browser=Browsers(Profiles())
    try:
        await browser.login('chatgpt')
        print('Normal Chrome opened. Complete ChatGPT sign-in, close that dedicated window, then verify from OnQ Agent. This helper does not declare authentication successful.',flush=True)
    finally: await browser.close()

if __name__=='__main__': asyncio.run(main())
