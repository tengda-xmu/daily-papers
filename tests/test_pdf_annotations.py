import hashlib
import uuid

import pymupdf as fitz

from connectors.codex_bridge.documents import parse_pdf
from connectors.codex_bridge.store import Store
from tests.test_codex_bridge import bridge, login, P1, P2


def source_pdf(rotation=0):
    with fitz.open() as pdf:
        page=pdf.new_page(width=500,height=700)
        page.insert_text((50,80),'Original paper for selection and translation.',fontsize=16)
        page.insert_text((50,115),'A reliable model requires independent validation.',fontsize=12)
        page.draw_rect((50,160,220,300),color=(0,.3,.6))
        page.set_rotation(rotation)
        pdf.new_page(width=500,height=700).insert_text((50,80),'Second page')
        return pdf.tobytes()


def prepare(app, rotation=0):
    store=app.state.store
    document=parse_pdf(source_pdf(rotation),store.directory(P1))
    store.set_document(P1,document)
    return document


def mark(kind='highlight', **values):
    return {'id':str(uuid.uuid4()),'page':1,'kind':kind,'rects':[[.1,.09,.6,.12]],'text':'Original paper','note':'核查原文依据','color':'yellow',**values}


def test_private_preview_annotation_export_preserves_original_and_unicode(bridge):
    client,app,_=bridge; h=login(client,app);doc=prepare(app);version='?version='+doc['hash'];base=f'/api/papers/{P1}'
    assert client.get(base+'/pdf'+version).status_code==401
    assert client.get(base+'/pdf'+version,headers={**h,'Origin':'https://untrusted.example'}).status_code==403
    response=client.get(base+'/pdf'+version,headers=h)
    original=response.content;assert response.status_code==200
    assert hashlib.sha256(original).hexdigest()[:16]==doc['hash']
    data=client.get(base+'/annotations'+version,headers=h).json();assert data['items']==[]
    kinds=[mark(),mark('underline'),mark('strikeout'),mark('line',points=[[.1,.2],[.5,.3]]),mark('ink',points=[[.2,.4],[.3,.41],[.4,.4]]),mark('note',points=[[.7,.3]])]
    data['items']=kinds
    saved=client.post(base+'/annotations',headers=h,json=data);assert saved.json()['revision']==1
    output=client.get(base+'/annotated-pdf'+version,headers=h);assert output.status_code==200
    with fitz.open(stream=output.content) as pdf:
        assert len(pdf)==2 and pdf[0].rect==fitz.Rect(0,0,500,700)
        assert 'Original paper' in pdf[0].get_text()
        annotations=list(pdf[0].annots());assert len(annotations)==6
        assert all(a.info['content']=='核查原文依据' for a in annotations)
    assert client.get(base+'/pdf'+version,headers=h).content==original
    restarted=Store(app.state.store.runtime,app.state.store.root)
    with restarted.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM annotations').fetchone()[0]==1
    assert client.delete(base,headers=h).status_code==200
    with restarted.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM annotations').fetchone()[0]==1
    assert client.get(base+'/pdf'+version,headers=h).content==original
    assert client.delete(f'/api/library/papers/{P1}',headers=h).status_code==200
    with restarted.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM annotations').fetchone()[0]==0


def test_annotations_reject_stale_tabs_document_replacement_and_wrong_paper(bridge):
    client,app,_=bridge;h=login(client,app);doc=prepare(app);base=f'/api/papers/{P1}'
    payload={'document_hash':doc['hash'],'revision':0,'items':[mark()]}
    assert client.post(base+'/annotations',headers=h,json=payload).status_code==200
    assert client.post(base+'/annotations',headers=h,json=payload).status_code==409
    assert client.get(f'/api/papers/{P2}/pdf?version='+doc['hash'],headers=h).status_code==409
    app.state.store.set_document(P1,{**doc,'hash':'f'*16})
    assert client.post(base+'/annotations',headers=h,json={**payload,'revision':1}).status_code==200
    assert client.get(base+'/pdf?version='+doc['hash'],headers=h).status_code==200
    assert client.get(base+'/annotations?version='+'f'*16,headers=h).json()['items']==[]


def test_annotation_geometry_and_page_validation(bridge):
    client,app,_=bridge;h=login(client,app);doc=prepare(app);base=f'/api/papers/{P1}/annotations'
    payload={'document_hash':doc['hash'],'revision':0}
    for item in [mark(rects=[[-.1,0,.2,.3]]),mark(rects=[[.8,.2,.1,.3]]),mark('ink',points=[]),mark(color='invalid')]:
        assert client.post(base,headers=h,json={**payload,'items':[item]}).status_code==422
    assert client.post(base,headers=h,json={**payload,'items':[mark(page=3)]}).status_code==400


def test_rotation_uses_display_coordinates_without_changing_page(bridge):
    client,app,_=bridge;h=login(client,app);doc=prepare(app,90);base=f'/api/papers/{P1}'
    payload={'document_hash':doc['hash'],'revision':0,'items':[mark('line',points=[[.1,.2],[.7,.8]])]}
    assert client.post(base+'/annotations',headers=h,json=payload).status_code==200
    output=client.get(base+'/annotated-pdf?version='+doc['hash'],headers=h).content
    with fitz.open(stream=output) as pdf:
        page=pdf[0];assert page.rotation==90 and page.rect.width==700
        annotation=next(page.annots())
        displayed=[fitz.Point(p)*page.rotation_matrix for p in annotation.vertices]
        assert abs(displayed[0].x/700-.1)<.001 and abs(displayed[0].y/500-.2)<.001
        assert abs(displayed[1].x/700-.7)<.001 and abs(displayed[1].y/500-.8)<.001
