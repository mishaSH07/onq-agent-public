"""Start one local worker; the OS releases the lock after crashes or shutdown."""
import os
import sys
import logging
from pathlib import Path
from filelock import FileLock,Timeout
import uvicorn
import asyncio

if __name__=='__main__':
    if '--convert-office' in sys.argv:
        from onq.convert_office import convert
        convert(Path(sys.argv[-2]),Path(sys.argv[-1]))
        sys.exit(0)
    state=Path(os.environ.get('ONQ_STATE',Path(os.environ.get('LOCALAPPDATA',Path.home()))/'onQ Library'))
    state.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(filename=state/'service.log',level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    try:
        with FileLock(state/'worker.lock',timeout=0):
            # File logs are compatible with pythonw (no terminal streams).
            async def serve():
                from onq.app import app
                server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8765,log_config=None,access_log=False))
                app.state.server=server
                await server.serve()
            asyncio.run(serve())
    except Timeout:
        sys.exit(0)
