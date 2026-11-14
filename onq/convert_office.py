"""Isolated Office conversion process; macros disabled, sources opened read-only."""
import sys
from pathlib import Path

def convert(source,target):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    app=None; document=None
    try:
        if source.suffix.lower()=='.pptx':
            app=win32com.client.DispatchEx('PowerPoint.Application')
            app.AutomationSecurity=3
            document=app.Presentations.Open(str(source),ReadOnly=True,Untitled=False,WithWindow=False)
            document.SaveAs(str(target),32)
        else:
            app=win32com.client.DispatchEx('Word.Application')
            app.Visible=False; app.DisplayAlerts=0; app.AutomationSecurity=3
            document=app.Documents.Open(str(source),ConfirmConversions=False,ReadOnly=True,AddToRecentFiles=False)
            document.ExportAsFixedFormat(str(target),17)
    finally:
        if document:
            try: document.Close()
            except Exception: pass
        if app:
            try: app.Quit()
            except Exception: pass
        pythoncom.CoUninitialize()

if __name__=='__main__': convert(Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve())
