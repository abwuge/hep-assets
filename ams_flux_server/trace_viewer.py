"""Build an offline conversation-and-tool replay from recorded trials."""

import argparse
import json
from pathlib import Path
import re


def content_text(value):
    if isinstance(value, list):
        return "\n".join(content_text(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") == "text":
            return content_text(value["text"])
        return json.dumps(value, ensure_ascii=False, indent=2)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return value
        if isinstance(parsed, dict):
            if "output" in parsed:
                result = f"status={parsed.get('status')} · exit_code={parsed.get('exit_code')}\n" + parsed["output"]
                return result
            if "text" in parsed:
                return str(parsed["text"])
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    return str(value)


def load_trial(directory):
    meta = json.loads((directory / "trial.json").read_text())
    items, calls = [], {}
    prompt = directory / "prompt.txt"
    if prompt.exists():
        items.append({"kind": "prompt", "label": "任务与检查反馈", "text": prompt.read_text()})
    transcript = directory / "claude.jsonl"
    for line in (transcript.read_text(errors="replace") if transcript.exists() else "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        blocks = event.get("message", {}).get("content", [])
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            kind = block.get("type")
            if kind in ("text", "thinking") and event.get("type") == "assistant":
                text = block.get("text", block.get("thinking", ""))
                if text.strip():
                    items.append({"kind": kind, "label": meta.get("model", "模型") if kind == "text" else "模型推理记录", "text": text})
            elif kind == "tool_use":
                arguments = block.get("input", {})
                entry = {"kind": "tool", "label": block["name"], "id": block["id"],
                         "input": json.dumps(arguments, ensure_ascii=False, indent=2),
                         "hint": arguments.get("command", arguments.get("path", "")),
                         "result": "未记录返回（可能在时限停止前发出）", "error": False}
                calls[block["id"]] = entry
                items.append(entry)
            elif kind == "tool_result" and block.get("tool_use_id") in calls:
                entry = calls[block["tool_use_id"]]
                entry["result"] = content_text(block.get("content", ""))
                entry["error"] = bool(block.get("is_error")) or "Error executing tool" in entry["result"]
                exit_code = re.search(r"exit_code=(-?\d+)", entry["result"])
                if "No such tool available" in entry["result"] or (exit_code and int(exit_code[1]) != 0):
                    entry["error"] = True
    assessment = directory / "assessment.json"
    findings = json.loads(assessment.read_text()) if assessment.exists() else {}
    completion = findings.get("completion")
    status_label = ("科学任务验收通过" if completion == "complete" else
                    "科学任务未完成" if completion == "incomplete" else "未记录科学验收")
    return {"name": directory.name, "meta": meta, "items": items, "status_label": status_label,
            "findings": findings, "path": str(directory.resolve())}


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>科研 Agent 工作回放</title>
<style>
:root{color-scheme:light dark;--bg:light-dark(#f7f7f5,#151716);--fg:light-dark(#222824,#e2e7e3);--muted:light-dark(#647069,#a1afa6);--panel:light-dark(#fff,#202521);--line:light-dark(#d9dfd9,#3b453e);--accent:light-dark(#236246,#83c6a3);--error:light-dark(#a23730,#f6a499)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1120px;margin:auto;padding:40px 28px}header{display:flex;justify-content:space-between;gap:20px;align-items:start}h1{font-size:30px;line-height:1.25;font-weight:600;margin:0 0 10px}p{margin:6px 0}small,.muted{color:var(--muted)}.route{font-family:ui-monospace,monospace;font-size:13px}.status{color:var(--error);white-space:nowrap}.controls{display:flex;gap:12px;flex-wrap:wrap;align-items:end;margin:28px 0 14px}label{display:flex;flex-direction:column;gap:5px;color:var(--muted);font-size:13px}select,input,button{font:inherit;background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:9px 12px}input{min-width:220px}button{cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}#meta{margin:8px 0 24px;color:var(--muted)}.findings{margin-bottom:24px}details{border-top:1px solid var(--line)}summary{cursor:pointer;list-style:none;padding:15px 0;display:flex;gap:12px;align-items:baseline}summary::before{content:'＋';color:var(--muted)}details[open]>summary::before{content:'−'}.number{font-variant-numeric:tabular-nums;color:var(--muted);min-width:28px;font-size:13px}.toolname{color:var(--accent);font-family:ui-monospace,monospace;font-size:13px}.hint{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:60%;font-size:13px}.error .toolname,.error .mark{color:var(--error)}.mark{margin-left:auto;white-space:nowrap;font-size:12px;color:var(--muted)}.content{padding:0 0 22px 53px}.blocklabel{color:var(--muted);font-size:12px;margin:12px 0 6px}pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;background:var(--panel);padding:16px;border-radius:7px;font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;tab-size:2}.prose{white-space:pre-wrap;overflow-wrap:anywhere;max-width:900px}.thinking{color:var(--muted)}.findings summary{font-weight:500}.empty{padding:30px 0;color:var(--muted)}a{color:var(--accent)}footer{margin-top:28px;padding-top:12px;border-top:1px solid var(--line);overflow-wrap:anywhere;font-size:12px;color:var(--muted)}@media(max-width:640px){main{padding:24px 16px}header{display:block}.status{margin-top:12px}h1{font-size:25px}label,input,select{width:100%}.controls{gap:8px}summary{flex-wrap:wrap;gap:6px}.hint{max-width:100%;flex-basis:100%;padding-left:43px;white-space:normal;overflow-wrap:anywhere}.content{padding-left:0}.mark{margin-left:0}.route{overflow-wrap:anywhere}}
</style></head><body><main>
<header><div><h1>科研 Agent 工作回放</h1><p class="route">Claude → SCP Hub → Task Server</p><p class="muted">按调用查看模型输出、命令、文件改动和返回结果。</p><p id="live-status" class="muted" role="status"></p></div><div id="scientific-status" class="status"></div></header>
<div class="controls"><label>试验轮次<select id="runs"></select></label><label>搜索记录<input id="search" type="search" placeholder="命令、路径、报错或文本"></label><label>内容<select id="filter"><option value="all">全部记录</option><option value="tool">工具调用</option><option value="error">调用错误</option><option value="thinking">模型推理记录</option></select></label><button id="collapse" type="button">收起全部</button></div>
<div id="meta" aria-live="polite"></div><details class="findings"><summary>本轮核查结果</summary><pre id="findings"></pre></details>
<section id="timeline" aria-label="工作记录"></section><footer id="source"></footer>
</main><script id="records" type="application/json">__DATA__</script><script>
let runs=JSON.parse(document.getElementById('records').textContent);
const live=__LIVE__, openness=new Map();let etag='',selectedName='';
const picker=document.getElementById('runs'), search=document.getElementById('search'), filter=document.getElementById('filter'), timeline=document.getElementById('timeline');
const titles={'agents-a1-trial':'首轮 · 估价上限停止','agents-a1-continuation':'续测 · 工具名问题','agents-a1-continuation-restart':'续测重启 · 简化工具名'};
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n}
function remember(){timeline.querySelectorAll('details[data-key]').forEach(d=>openness.set(d.dataset.key,d.open))}
function install(next){remember();runs=next;picker.replaceChildren();runs.forEach((r,i)=>{let o=el('option','',titles[r.name]||r.name);o.value=String(i);picker.append(o)});let index=runs.findIndex(r=>r.name===selectedName);picker.value=String(index<0?runs.length-1:index);render()}
function render(){remember();const r=runs[Number(picker.value)],q=search.value.toLowerCase(),mode=filter.value;timeline.replaceChildren();let count=0;
 if(!r){document.getElementById('meta').textContent='尚无试验记录';document.getElementById('findings').textContent='尚无核查记录';document.getElementById('source').textContent='';document.getElementById('scientific-status').textContent='';timeline.append(el('p','empty','新试验写入记录后会自动出现在这里。'));return}selectedName=r.name;const calls=r.items.filter(x=>x.kind==='tool');
 document.getElementById('findings').textContent=Object.keys(r.findings).length?JSON.stringify(r.findings,null,2):'本轮尚未保存独立核查结果。';
 document.getElementById('scientific-status').textContent=r.status_label;
 document.getElementById('source').textContent='原始记录：'+r.path+'/claude.jsonl';
 r.items.forEach((item,index)=>{if(mode==='tool'&&item.kind!=='tool'||mode==='error'&&!item.error||mode==='thinking'&&item.kind!=='thinking')return;if(q&&!JSON.stringify(item).toLowerCase().includes(q))return;count++;
 const d=el('details',item.error?'error':item.kind==='thinking'?'thinking':'');d.dataset.key=r.name+':'+(item.id||index);d.open=openness.has(d.dataset.key)?openness.get(d.dataset.key):item.kind==='text';const s=el('summary');s.append(el('span','number',String(index+1).padStart(3,'0')),el('span',item.kind==='tool'?'toolname':'',item.label));if(item.hint)s.append(el('span','hint',item.hint));if(item.kind==='tool')s.append(el('span','mark',item.error?'调用报错':'工具记录'));d.append(s);const c=el('div','content');
 if(item.kind==='tool'){c.append(el('div','blocklabel','调用参数'),el('pre','',item.input),el('div','blocklabel','返回结果'),el('pre','',item.result));}else{c.append(el('div','prose',item.text))}d.append(c);timeline.append(d)});
 if(!count)timeline.append(el('p','empty','没有匹配的记录'));
 const reason=r.meta.stopped_reason||r.meta.client_result?.terminal_reason||(r.meta.ended_at?'已结束':'记录未结束');const labels={wall_time_limit:'达到时限',budget_exhausted:'达到客户端估价上限',supervisor_restart_after_repeated_invalid_tool_names:'工具名问题后重启'};
 document.getElementById('meta').textContent=`${r.meta.model} · ${calls.length} 次工具调用 · ${labels[reason]||reason} · 显示 ${count} 条记录`;
}
async function refresh(){try{const response=await fetch('/api/replay',{headers:etag?{'If-None-Match':etag}:{},cache:'no-store'});if(response.status!==304){if(!response.ok)throw new Error('HTTP '+response.status);const data=await response.json();etag=response.headers.get('ETag')||'';install(data.trials)}document.getElementById('live-status').textContent='自动更新 · 每 5 秒读取记录';}catch(error){document.getElementById('live-status').textContent='更新暂不可用，保留当前记录；5 秒后重试。'}finally{setTimeout(refresh,5000)}}
picker.addEventListener('change',render);search.addEventListener('input',render);filter.addEventListener('change',render);document.getElementById('collapse').addEventListener('click',()=>timeline.querySelectorAll('details').forEach(d=>d.open=false));install(runs);if(live)refresh();
</script></body></html>'''


def render_html(trials, live=False):
    data = json.dumps(trials, ensure_ascii=False).replace("<", "\\u003c")
    return TEMPLATE.replace("__LIVE__", str(live).lower()).replace("__DATA__", data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trials", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.write_text(render_html([load_trial(path) for path in args.trials]))
    print(args.output.resolve())


if __name__ == "__main__":
    main()
