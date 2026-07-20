import sys, json, re, torch
from PIL import Image
from transformers import AutoModelForImageTextToText, AutoProcessor
from peft import PeftModel
BASE="Qwen/Qwen3-VL-2B-Instruct"; ADAPTER="/out/qwen3vl-cad-lora"
PROMPT=("Ты — векторизатор технических чертежей (ЕСКД). На изображении инженерный "
 "чертёж. Верни геометрические примитивы САМОЙ детали в системе координат "
 "0..1000 (обе оси масштабированы одинаково по большей стороне, 0,0 — левый "
 "верх). Игнорируй размерные/выносные линии, текст, рамку и штамп. Ответ — "
 "строго JSON: {\"lines\":[[x1,y1,x2,y2]],\"circles\":[[cx,cy,r]],"
 "\"arcs\":[[cx,cy,r,start_deg,end_deg]],\"polylines\":[{\"pts\":[[x,y]],\"closed\":0}]}")
model=AutoModelForImageTextToText.from_pretrained(BASE,torch_dtype=torch.bfloat16,device_map="cuda")
model=PeftModel.from_pretrained(model,ADAPTER); proc=AutoProcessor.from_pretrained(BASE)
def dsl(path):
    img=Image.open(path).convert("RGB"); img.thumbnail((1024,1024))
    msgs=[{"role":"user","content":[{"type":"image"},{"type":"text","text":PROMPT}]}]
    text=proc.apply_chat_template(msgs,tokenize=False,add_generation_prompt=True)
    inp=proc(text=[text],images=[img],return_tensors="pt").to("cuda")
    out=model.generate(**inp,max_new_tokens=3072,do_sample=False)
    g=proc.batch_decode(out[:,inp.input_ids.shape[1]:],skip_special_tokens=True)[0]
    g=re.sub(r'<think>.*?</think>','',g,flags=re.S)
    g=re.sub(r'^```(?:json)?|```$','',g.strip(),flags=re.M).strip()
    s,e=g.find('{'),g.rfind('}')
    try: return json.loads(g[s:e+1])
    except: return {}
man=[json.loads(l) for l in open(sys.argv[1])]
out={}
for m in man:
    if m['split']!='holdout': continue
    out[m['ir']]=dsl(m['image'])
json.dump(out, open(sys.argv[2],'w'))
print("dumped", len(out), "holdout DSLs")
