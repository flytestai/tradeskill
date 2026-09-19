import json, secrets, urllib.request
url = "https://bee-ai.integrity.com.cn/skills/v1/query2data"
def q(query):
    payload={"query":query,"page":"1","limit":"5","is_cache":"1","expand_index":"true"}
    headers={"Content-Type":"application/json","X-Claw-Call-Type":"normal","X-Claw-Skill-Id":"hithink-zhishu-query",
      "X-Claw-Skill-Version":"1.0.0","X-Claw-Plugin-Id":"none","X-Claw-Plugin-Version":"none","X-Claw-Trace-Id":secrets.token_hex(32)}
    req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers=headers,method="POST")
    try: return json.loads(urllib.request.urlopen(req,timeout=40).read().decode())
    except Exception as e: return {"error":str(e)}
for qq in ["上证指数2026年最高价","上证指数2026年至今最高点","创业板指2026年最高价"]:
    r=q(qq); ds=r.get("datas") or []
    print(f"\n== {qq} == n={len(ds)}")
    for d in ds[:2]:
        if isinstance(d,dict):
            for k,v in d.items(): print(f"   {k} = {v}")
            print("  ---")
