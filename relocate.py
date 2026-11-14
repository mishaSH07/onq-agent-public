import sys
import os
from pathlib import Path
from filelock import FileLock
from onq.store import Store,validate_library

if __name__=='__main__':
    state=Path(os.environ.get('ONQ_STATE',Path(os.environ.get('LOCALAPPDATA',Path.home()))/'onQ Library'))
    state.mkdir(parents=True,exist_ok=True)
    with FileLock(state/'worker.lock',timeout=0):
        s=Store(state)
        target=validate_library(sys.argv[1])
        missing=[v['path'] for v in s.rows('SELECT path FROM versions') if not (target/v['path']).is_file()]
        if missing: raise SystemExit(f'The destination is missing {len(missing)} originals. Copy the complete library first.')
        s.set_setting('library',str(target))
        s.set_setting('cloud_verified',False)
        print('Library path updated. Sign in again on this computer before syncing.')
