"""Dedicated profiles, never the user's everyday Chrome profile."""
from playwright.async_api import async_playwright
import json
import os
import subprocess
from pathlib import Path

def chrome_executable():
    for root in (os.environ.get('PROGRAMFILES',''),os.environ.get('PROGRAMFILES(X86)',''),os.environ.get('LOCALAPPDATA','')):
        candidate=Path(root)/'Google'/'Chrome'/'Application'/'chrome.exe'
        if candidate.is_file(): return candidate
    raise ActionNeeded('Install Google Chrome to use dedicated sign-in windows.')

def manual_chrome_command(profile,url):
    return [str(chrome_executable()),'--user-data-dir='+str(profile),'--new-window',url]

class SignInNeeded(Exception): pass
class ActionNeeded(Exception): pass

class SharedBrowserBlocker(ActionNeeded):
    """User action in dedicated Chrome is needed before other projects can proceed."""


class Browsers:
    def __init__(self,store):
        self.store=store; self.pw=None; self.contexts={}; self.manual={}

    async def context(self,service,visible=False):
        manual=self.manual.get(service)
        if manual and manual.poll() is None:
            raise SharedBrowserBlocker('Finish sign-in in the normal Chrome window, close that dedicated window, then retry.')
        if self.pw is None: self.pw=await async_playwright().start()
        existing=self.contexts.get(service)
        if existing:
            try:
                if existing.pages: return existing
            except Exception: pass
            self.contexts.pop(service,None)
        # Headed profiles support interactive 2FA and persist sessions between runs.
        try:
            ctx=await self.pw.chromium.launch_persistent_context(
                str(self.store.state/'profiles'/('chrome-'+service)),channel='chrome',headless=False,
                accept_downloads=True, chromium_sandbox=True, viewport={'width':1280,'height':860},
                args=['--no-first-run'])
        except Exception as exc:
            raise SharedBrowserBlocker('Chrome could not open the dedicated profile. Close its other dedicated sign-in windows and retry. Your ordinary Chrome windows can stay open.') from exc
        self.contexts[service]=ctx
        session=self.store.state/'profiles'/('chrome-'+service+'.session')
        if session.exists() and os.name=='nt':
            try:
                import win32crypt
                saved=json.loads(win32crypt.CryptUnprotectData(session.read_bytes(),None,None,None,0)[1])
                present={(c['name'],c['domain'],c['path']) for c in await ctx.cookies()}
                await ctx.add_cookies([c for c in saved.get('cookies',[]) if (c['name'],c['domain'],c['path']) not in present])
            except Exception: pass # Sign-in is the recovery path for invalid/expired state.
        ctx.on('close',lambda: self.contexts.pop(service,None))
        return ctx

    async def login(self,service,project_sources=None):
        if service=='chatgpt':
            existing=self.manual.get(service)
            if existing and existing.poll() is None:
                raise ActionNeeded('The dedicated normal Chrome sign-in window is already open. Finish sign-in and close it before verifying.')
            ctx=self.contexts.get(service)
            if ctx: await ctx.close()
            profile=self.store.state/'profiles'/('chrome-'+service)
            profile.mkdir(parents=True,exist_ok=True)
            snapshot=self.store.state/'profiles'/('chrome-'+service+'.session')
            if snapshot.exists(): snapshot.replace(snapshot.with_suffix('.before-manual'))
            self.manual[service]=subprocess.Popen(manual_chrome_command(profile,project_sources or 'https://chatgpt.com/'),
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            return
        ctx=await self.context(service,True)
        page=ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto('https://onq.queensu.ca/d2l/home' if service=='onq' else 'https://chatgpt.com/',wait_until='domcontentloaded')
        await page.bring_to_front()

    async def close(self):
        for service,ctx in list(self.contexts.items()):
            try:
                if os.name=='nt':
                    import win32crypt
                    saved=await ctx.storage_state()
                    encrypted=win32crypt.CryptProtectData(json.dumps(saved).encode(),'OnQ Agent session',None,None,None,0)
                    (self.store.state/'profiles'/('chrome-'+service+'.session')).write_bytes(encrypted)
                await ctx.close()
            except Exception: pass
        if self.pw: await self.pw.stop()

async def check_onq(page):
    if 'onq.queensu.ca' not in page.url or '/d2l/' not in page.url:
        raise SignInNeeded('Sign in to onQ in the dedicated browser, then retry.')
    if await page.locator('input[type=password]').count():
        raise SignInNeeded('onQ needs sign-in.')
