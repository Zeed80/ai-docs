import json, sys
sys.path.insert(0,'/home/project/document-invoices-ai_codex/backend')
from app.ai.cad_ir.schema import CadIR, Segment, Circle, Arc, Polyline, Point
from app.ai.cad_entity_metrics import compare_entities

def dsl_to_entities(d, scale):
    ents=[]
    for L in d.get('lines',[]):
        if len(L)==4: ents.append(Segment(p1=Point(x=L[0]*scale,y=L[1]*scale),p2=Point(x=L[2]*scale,y=L[3]*scale)))
    for C in d.get('circles',[]):
        if len(C)==3: ents.append(Circle(center=Point(x=C[0]*scale,y=C[1]*scale),radius=C[2]*scale))
    for A in d.get('arcs',[]):
        if len(A)==5: ents.append(Arc(center=Point(x=A[0]*scale,y=A[1]*scale),radius=A[2]*scale,start_angle=A[3],end_angle=A[4]))
    for P in d.get('polylines',[]):
        pts=[Point(x=p[0]*scale,y=p[1]*scale) for p in P.get('pts',[]) if len(p)==2]
        if len(pts)>=2: ents.append(Polyline(points=pts,closed=bool(P.get('closed'))))
    return ents

data=json.load(open(sys.argv[1]))
tp=fp=fn=0
for ir_path,d in data.items():
    truth=CadIR.model_validate_json(open(ir_path).read())
    W,H=truth.source.image_width,truth.source.image_height
    scale=max(W,H)/1000.0
    ents=dsl_to_entities(d,scale)
    m=compare_entities(ents, truth.entities, predicted_size=(W,H), truth_size=(W,H))
    mi=m['micro']; tp+=mi['matched']; fp+=mi['false_positive']; fn+=mi['false_negative']
    print('  %-40s pred=%d gt_matched=%d fp=%d fn=%d'%(ir_path.split('/')[-1][:40],len(ents),mi['matched'],mi['false_positive'],mi['false_negative']))
P=tp/(tp+fp) if tp+fp else 0; R=tp/(tp+fn) if tp+fn else 0; F=2*P*R/(P+R) if P+R else 0
print('GENERATIVE VLM entity: P=%.3f R=%.3f F1=%.3f matched=%d (CV baseline F1=0.186)'%(P,R,F,tp))
