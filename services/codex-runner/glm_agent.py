#!/usr/bin/env python3
import argparse, json, os, sys, urllib.request, urllib.error

def main():
    ap=argparse.ArgumentParser(description='Run GLM as max-permission agent through codex-mobile-runner')
    ap.add_argument('--workspace','-C',default=os.getcwd())
    ap.add_argument('--max-commands',type=int,default=8)
    ap.add_argument('task',nargs='*')
    ns=ap.parse_args()
    task=' '.join(ns.task).strip() or sys.stdin.read().strip()
    if not task:
        print('usage: glm-agent [-C workspace] 任务', file=sys.stderr); return 2
    token=os.environ.get('CODEX_RUNNER_TOKEN','')
    data=json.dumps({'workspace':ns.workspace,'task':task,'max_commands':ns.max_commands},ensure_ascii=False).encode()
    headers={'Content-Type':'application/json'}
    if token: headers['Authorization']='Bearer '+token
    req=urllib.request.Request('http://127.0.0.1:8787/glm_run', data=data, headers=headers, method='POST')
    try:
        raw=urllib.request.urlopen(req, timeout=1200).read().decode('utf-8','replace')
        obj=json.loads(raw)
        print(obj.get('text') or obj.get('final') or json.dumps(obj,ensure_ascii=False,indent=2))
        return 0 if obj.get('ok') else 1
    except urllib.error.HTTPError as e:
        print(e.read().decode('utf-8','replace'), file=sys.stderr); return 3
    except Exception as e:
        print(type(e).__name__+': '+str(e), file=sys.stderr); return 4
if __name__=='__main__':
    raise SystemExit(main())
