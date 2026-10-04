"""Extract the current official catalog for review; never edits ~/.codex.

Only contacts DeepSeek's public documentation. Does not call the paid model API.
Upstream catalog contains vendor metadata/prompts; retain the source manifest.
"""
import argparse,hashlib,json
from datetime import datetime,timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import build_opener,HTTPRedirectHandler

URL='https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/codex/'
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('unexpected_document_redirect')
class PreBlocks(HTMLParser):
    def __init__(self):super().__init__();self.depth=0;self.blocks=[];self.current=[]
    def handle_starttag(self,tag,attrs):
        if tag=='pre':self.depth+=1;self.current=[]
    def handle_data(self,data):
        if self.depth:self.current.append(data)
    def handle_endtag(self,tag):
        if tag=='pre' and self.depth:
            self.depth-=1;self.blocks.append(''.join(self.current))
def extract(html):
    p=PreBlocks();p.feed(html)
    for b in p.blocks:
        try:value=json.loads(b)
        except (ValueError,TypeError):continue
        if isinstance(value,dict) and isinstance(value.get('models'),list):
            if any(isinstance(m,dict) and m.get('slug')=='deepseek-flash' for m in value['models']):return value
    raise ValueError('official_catalog_not_found_review_doc_manually')
def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--html',help='Optional previously saved official HTML');a=p.parse_args()
    target=Path(a.out)
    if target.exists():p.error('Output exists; use a new versioned filename and review before replacing.')
    if a.html:raw=Path(a.html).read_bytes();source='local_html_unverified'
    else:
        with build_opener(NoRedirect).open(URL,timeout=25) as r:raw=r.read(4_000_001)
        source=URL
    if len(raw)>4_000_000:raise ValueError('document_too_large')
    catalog=extract(raw.decode('utf-8'));target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n')
    meta={'source':source,'retrieved_at':datetime.now(timezone.utc).isoformat(),
          'source_sha256':hashlib.sha256(raw).hexdigest(),'catalog_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
          'models':[{'slug':m.get('slug'),'minimal_client_version':m.get('minimal_client_version')} for m in catalog['models']],
          'codex_configuration_changed':False,'live_api_verified':False}
    target.with_suffix('.source.json').write_text(json.dumps(meta,indent=2)+'\n');print(json.dumps(meta))
if __name__=='__main__':main()
