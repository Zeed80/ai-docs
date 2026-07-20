import sys, json, re, io
import torch
from PIL import Image, ImageDraw
from transformers import AutoModelForImageTextToText, AutoProcessor
from peft import PeftModel

BASE="Qwen/Qwen3-VL-2B-Instruct"; ADAPTER="/out/qwen3vl-cad-lora"
PROMPT=("Ты — векторизатор технических чертежей (ЕСКД). На изображении инженерный "
 "чертёж. Верни геометрические примитивы САМОЙ детали в системе координат "
 "0..1000 (обе оси масштабированы одинаково по большей стороне, 0,0 — левый "
 "верх). Игнорируй размерные/выносные линии, текст, рамку и штамп. Ответ — "
 "строго JSON: {\"lines\":[[x1,y1,x2,y2]],\"circles\":[[cx,cy,r]],"
 "\"arcs\":[[cx,cy,r,start_deg,end_deg]],"
 "\"polylines\":[{\"pts\":[[x,y]],\"closed\":0}]}")

model=AutoModelForImageTextToText.from_pretrained(BASE,torch_dtype=torch.bfloat16,device_map="cuda")
model=PeftModel.from_pretrained(model,ADAPTER)
proc=AutoProcessor.from_pretrained(BASE)

def infer(path,out):
    img=Image.open(path).convert("RGB"); W,H=img.size
    small=img.copy(); small.thumbnail((1024,1024))
    msgs=[{"role":"user","content":[{"type":"image"},{"type":"text","text":PROMPT}]}]
    text=proc.apply_chat_template(msgs,tokenize=False,add_generation_prompt=True)
    inp=proc(text=[text],images=[small],return_tensors="pt").to("cuda")
    out_ids=model.generate(**inp,max_new_tokens=3072,do_sample=False)
    gen=proc.batch_decode(out_ids[:,inp.input_ids.shape[1]:],skip_special_tokens=True)[0]
    m=re.sub(r'^```(?:json)?|```$','',gen.strip(),flags=re.M).strip()
    s,e=m.find('{'),m.rfind('}')
    try: d=json.loads(m[s:e+1])
    except Exception as ex: print("PARSE FAIL:",gen[:300]); return
    nl,nc,na,npl=len(d.get("lines",[])),len(d.get("circles",[])),len(d.get("arcs",[])),len(d.get("polylines",[]))
    print(f"{path.split('/')[-1]}: lines={nl} circles={nc} arcs={na} polylines={npl}")
    S=max(W,H)/1000.0; canvas=Image.new("RGB",(W,H),"white"); dr=ImageDraw.Draw(canvas)
    for L in d.get("lines",[]):
        if len(L)==4: dr.line((L[0]*S,L[1]*S,L[2]*S,L[3]*S),fill="black",width=2)
    for C in d.get("circles",[]):
        if len(C)==3: r=C[2]*S; dr.ellipse([C[0]*S-r,C[1]*S-r,C[0]*S+r,C[1]*S+r],outline="black",width=2)
    for A in d.get("arcs",[]):
        if len(A)==5: r=A[2]*S; dr.arc([A[0]*S-r,A[1]*S-r,A[0]*S+r,A[1]*S+r],A[3],A[4],fill="black",width=2)
    for P in d.get("polylines",[]):
        pts=[(p[0]*S,p[1]*S) for p in P.get("pts",[])]
        if P.get("closed") and len(pts)>2: pts=pts+[pts[0]]
        if len(pts)>=2: dr.line(pts,fill="black",width=2)
    comp=Image.new("RGB",(W*2+20,H),"white"); comp.paste(img,(0,0)); comp.paste(canvas,(W+20,0)); comp.save(out)
    print("saved",out)

infer(sys.argv[1], sys.argv[2])
infer(sys.argv[3], sys.argv[4])
