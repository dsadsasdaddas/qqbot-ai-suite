#!/usr/bin/env python3
import json, os, sys, urllib.request, urllib.error
from pathlib import Path

def normalize_key(value):
    if isinstance(value, list):
        for item in value:
            item=str(item or '').strip()
            if item:
                return item
        return ''
    return str(value or '').strip()

def load_provider():
    for p in [Path('/host/home/wzu/qqbot/data/cmd_config.json'), Path('/home/wzu/qqbot/data/cmd_config.json')]:
        if not p.exists():
            continue
        data=json.loads(p.read_text(encoding='utf-8-sig', errors='replace'))
        providers=data.get('provider') or []
        chosen=None
        for item in providers:
            if item.get('id')=='glm5_default': chosen=item; break
        if chosen is None:
            for item in providers:
                if 'zhipu' in str(item.get('type','')).lower() or 'bigmodel' in str(item.get('api_base','')).lower():
                    chosen=item; break
        if chosen:
            mc=chosen.get('model_config') or {}
            return normalize_key(chosen.get('key')), (chosen.get('api_base') or 'https://open.bigmodel.cn/api/paas/v4/'), mc.get('model') or 'glm-5.2', mc.get('temperature',0.7)
    return '', 'https://open.bigmodel.cn/api/paas/v4/', 'glm-5.2', 0.7

def main():
    prompt=' '.join(sys.argv[1:]).strip() or sys.stdin.read().strip()
    if not prompt:
        print('usage: glm-assist 你的问题   或 echo 问题 | glm-assist', file=sys.stderr); return 2
    key,base,model,temp=load_provider()
    key=os.environ.get('GLM_API_KEY') or key
    if not key:
        print('GLM key not found', file=sys.stderr); return 3
    url=base.rstrip('/')+'/chat/completions'
    body={'model':model,'messages':[{'role':'user','content':prompt}], 'temperature': temp}
    req=urllib.request.Request(url, data=json.dumps(body,ensure_ascii=False).encode(), headers={'Content-Type':'application/json','Authorization':'Bearer '+key}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=int(os.environ.get('GLM_ASSIST_TIMEOUT','180'))) as r:
            raw=r.read().decode('utf-8','replace')
        data=json.loads(raw)
        print((((data.get('choices') or [{}])[0].get('message') or {}).get('content') or '').strip())
        return 0
    except urllib.error.HTTPError as e:
        print('HTTP', e.code, e.read().decode('utf-8','replace')[:1000], file=sys.stderr); return 4
    except Exception as e:
        print(type(e).__name__+': '+str(e), file=sys.stderr); return 5
if __name__=='__main__':
    raise SystemExit(main())
