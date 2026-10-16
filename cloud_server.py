from pathlib import Path
import gzip
import os
import shutil
import sqlite3
import sys

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'backend/data'
DATA.mkdir(parents=True,exist_ok=True)
DB=DATA/'app.db'
if not DB.exists():
    with gzip.open(ROOT/'seed/platform-cache.sqlite.gz','rb') as source,DB.open('wb') as target:
        shutil.copyfileobj(source,target)
    with sqlite3.connect(DB) as db:
        db.execute("UPDATE datasets SET html_zip=?,attachment_zip=NULL WHERE id=?",
                   (str(ROOT/'seed/official_html.zip'),'ds_20261014_43c21f'))
os.environ['ICT_DATA_DIR']=str(DATA)
os.environ['ICT_GOLD_DIR']=str(ROOT/'gold/full_verified')
os.environ.setdefault('LLM_PROVIDER','deepseek')
os.environ.setdefault('DEEPSEEK_MODEL','deepseek-flash')
os.environ.setdefault('LLM_TIMEOUT_SECONDS','180')
os.environ.setdefault('LLM_MAX_CONCURRENCY','3')
sys.path.insert(0,str(ROOT/'backend'))
if __name__=='__main__':
    from app import cloud_runtime
    cloud_runtime.install()
    from app.main import app
    cloud_runtime.connect_app(app)
    import uvicorn
    uvicorn.run(app,host='0.0.0.0',port=int(os.environ.get('PORT','3000')),access_log=False)
