import hashlib
import json
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


def test_draft_export_includes_unsaved_changes_without_persisting_or_changing_source(bridge):
    client,app,_=bridge;h=login(client,app);doc=prepare(app,90);base=f'/api/papers/{P1}'
    saved=mark(note='Previously saved')
    assert client.post(base+'/annotations',headers=h,json={'document_hash':doc['hash'],'revision':0,'items':[saved]}).status_code==200
    original=client.get(base+'/pdf?version='+doc['hash'],headers=h).content
    baseline=client.get(base+'/annotations?version='+doc['hash'],headers=h).json()
    # A newer source must not redirect an export of this specific saved PDF version.
    replacement=parse_pdf(source_pdf()+b'\n% new source\n',app.state.store.directory(P1))
    app.state.store.set_document(P1,replacement)
    files={p.relative_to(app.state.store.runtime) for p in app.state.store.runtime.rglob('*') if p.is_file()}
    draft=[{**saved,'note':'Unsaved edit'},mark('note',points=[[.5,.5]],note='New draft note')]
    response=client.post(base+'/annotated-pdf',headers=h,json={'document_hash':doc['hash'],'items':draft})
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    with fitz.open(stream=response.content) as pdf:
        assert len(pdf)==2 and pdf[0].rotation==90 and pdf[0].rect==fitz.Rect(0,0,700,500)
        assert 'Original paper' in pdf[0].get_text()
        assert [a.info['content'] for a in pdf[0].annots()]==['Unsaved edit','New draft note']
    assert client.get(base+'/annotations?version='+doc['hash'],headers=h).json()==baseline
    assert client.get(base+'/pdf?version='+doc['hash'],headers=h).content==original
    assert app.state.store.document(P1)['hash']==replacement['hash']
    assert {p.relative_to(app.state.store.runtime) for p in app.state.store.runtime.rglob('*') if p.is_file()}==files
    with fitz.open(stream=client.get(base+'/annotated-pdf?version='+doc['hash'],headers=h).content) as pdf:
        assert [a.info['content'] for a in pdf[0].annots()]==['Previously saved']


def test_draft_export_enforces_pairing_version_and_annotation_validation(bridge):
    client,app,_=bridge;h=login(client,app);doc=prepare(app);base=f'/api/papers/{P1}/annotated-pdf'
    payload={'document_hash':doc['hash'],'items':[mark()]}
    assert client.post(base,json=payload).status_code==401
    assert client.post(base,headers={**h,'Origin':'https://untrusted.example'},json=payload).status_code==403
    assert client.post(f'/api/papers/{P2}/annotated-pdf',headers=h,json=payload).status_code==409
    assert client.post(base,headers=h,json={**payload,'document_hash':'x'}).status_code==422
    for items in [[mark(page=3)],[mark(rects=[[-.1,0,.2,.3]])],[payload['items'][0]]*2]:
        result=client.post(base,headers=h,json={**payload,'items':items})
        assert result.status_code==(400 if items[0]['page']==3 else 422)
    assert client.get(f'/api/papers/{P1}/annotations?version='+doc['hash'],headers=h).json()['revision']==0


def test_draft_export_uses_annotation_body_limit_without_creating_records(bridge):
    client,app,_=bridge;h=login(client,app);doc=prepare(app);base=f'/api/papers/{P1}/annotated-pdf'
    payload={'document_hash':doc['hash'],'items':[mark(note='x'*4000) for _ in range(20)]}
    assert len(json.dumps(payload))>65536
    assert client.post(base,headers=h,json=payload).status_code==200
    assert client.post(base,headers=h,content=b' '*(2*1024*1024+1)).status_code==413
    with app.state.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM annotations').fetchone()[0]==0
