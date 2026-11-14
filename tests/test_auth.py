from pathlib import Path
from onq.browser import manual_chrome_command

def test_normal_signin_uses_dedicated_chrome_without_automation(monkeypatch,tmp_path):
    monkeypatch.setattr('onq.browser.chrome_executable',lambda:Path('C:/Chrome/chrome.exe'))
    command=manual_chrome_command(tmp_path/'dedicated','https://chatgpt.com/')
    assert command[0].endswith('chrome.exe')
    assert '--user-data-dir='+str(tmp_path/'dedicated') in command
    assert command[-1]=='https://chatgpt.com/'
    assert not any('remote-debugging' in arg or 'disable' in arg or 'automation' in arg for arg in command)
