import json
import sqlite3

from connectors.codex_bridge.store import Store
from tests.test_codex_bridge import bridge, login, ask, P1, P2, screenshot_bytes, translation_doc


def select(c, h, store, message_id, paper=P1, expected=None):
    return c.post(f'/api/papers/{paper}/branch', headers=h, json={'message_id':message_id,
        'expected_leaf':store.state(paper)['active_leaf'] if expected is None else expected})


def test_edit_regenerate_and_branch_switch_preserve_future_and_rebuild_exact_prefix(bridge):
    c, app, rpc = bridge; h=login(c,app); store=app.state.store
    ask(c,h,message='ORIGINAL-FIRST')
    first=store.history(P1); user,answer=first
    ask(c,h,message='OLD-FUTURE')
    original=store.history(P1); old_thread=rpc.threads[-1]
    response=ask(c,h,message='EDITED-FIRST', edit_message_id=user['id'], expected_leaf=original[-1]['id'])
    assert '"status": "completed"' in response.text
    edited=store.history(P1)
    assert len(edited)==2 and edited[0]['content']=='EDITED-FIRST'
    assert edited[0]['versions']==[user['id'],edited[0]['id']]
    assert len(store.history(P1,all_versions=True))==6
    assert rpc.threads[-1]!=old_thread
    assert 'EDITED-FIRST' in rpc.inputs[-1][1]
    assert 'ORIGINAL-FIRST' not in rpc.inputs[-1][1] and 'OLD-FUTURE' not in rpc.inputs[-1][1]
    assert select(c,h,store,user['id']).status_code==200
    assert [r['id'] for r in store.history(P1)]==[r['id'] for r in original]
    response=ask(c,h,regenerate_message_id=answer['id'], expected_leaf=original[-1]['id'], model='other-model')
    regenerated=store.history(P1)
    assert '"status": "completed"' in response.text
    assert len(regenerated)==2 and regenerated[0]['id']==user['id']
    assert regenerated[-1]['versions']==[answer['id'],regenerated[-1]['id']]
    assert regenerated[-1]['model']=='other-model'
    assert 'ORIGINAL-FIRST' in rpc.inputs[-1][1] and 'OLD-FUTURE' not in rpc.inputs[-1][1]
    assert select(c,h,store,answer['id']).status_code==200
    assert [r['id'] for r in store.history(P1)]==[r['id'] for r in original]
    ask(c,h,message='FOLLOW-OLD-BRANCH',expected_leaf=original[-1]['id'])
    prompt=rpc.inputs[-1][1]
    assert 'ORIGINAL-FIRST' in prompt and 'OLD-FUTURE' in prompt and 'EDITED-FIRST' not in prompt
    assert select(c,h,store,edited[0]['id']).status_code==200
    assert [r['id'] for r in store.history(P1)]==[r['id'] for r in edited]
    restarted=Store(store.runtime,store.root)
    assert [r['id'] for r in restarted.history(P1)]==[r['id'] for r in edited]
    # Explicit downloads remain scoped to their original paper even when hidden.
    assert c.get(f'/api/papers/{P1}/export-pdf?message_id={answer["id"]}',headers=h).status_code==200
    assert c.get(f'/api/papers/{P2}/export-pdf?message_id={answer["id"]}',headers=h).status_code==404


def test_stale_tabs_cross_paper_and_replaced_documents_do_not_mutate_history(bridge):
    c,app,rpc=bridge;h=login(c,app);store=app.state.store
    ask(c,h,message='first'); a=store.history(P1)
    ask(c,h,paper=P2,message='another paper'); b=store.history(P2)
    calls=len(rpc.inputs)
    assert ask(c,h,edit_message_id=b[0]['id'],expected_leaf=a[-1]['id']).status_code==404
    assert ask(c,h,edit_message_id=a[0]['id'],expected_leaf=0).status_code==409
    assert ask(c,h,edit_message_id=a[0]['id']).status_code==400
    assert select(c,h,store,b[0]['id']).status_code==404
    assert select(c,h,store,a[0]['id'],expected=0).status_code==409
    assert c.post(f'/api/papers/{P1}/branch',json={'message_id':a[0]['id'],'expected_leaf':a[-1]['id']}).status_code==401
    store.set_document(P1,translation_doc(1))
    assert ask(c,h,regenerate_message_id=a[-1]['id'],expected_leaf=a[-1]['id']).status_code==409
    assert len(rpc.inputs)==calls and len(store.history(P1,all_versions=True))==2


def test_revising_screenshot_translation_keeps_image_and_language(bridge):
    c,app,rpc=bridge;h=login(c,app);store=app.state.store
    response=c.post(f'/api/papers/{P1}/screenshots',headers=h,files={'file':('sample.png',screenshot_bytes(),'image/png')})
    assert response.status_code == 200
    image=response.json()
    ask(c,h,message='Translate visible words',mode='translate',translation_source='image',translation_target='en',attachment_ids=[image['id']])
    rows=store.history(P1)
    response=ask(c,h,message='Translate only the heading',edit_message_id=rows[0]['id'],expected_leaf=rows[-1]['id'])
    assert '"status": "completed"' in response.text
    row=store.history(P1)[0]
    assert row['request']['translation_source']=='image' and row['request']['translation_target']=='en'
    assert row['attachments']==[image]
    assert rpc.inputs[-1][2] and '目标语言：英文' in rpc.inputs[-1][1]


def test_regenerating_completed_full_translation_calls_model_instead_of_replaying_cache(bridge):
    c,app,rpc=bridge;h=login(c,app);store=app.state.store
    store.set_document(P1,translation_doc(2))
    ask(c,h,mode='translate',translation_source='full')
    first=store.history(P1)[-1];count=len(rpc.inputs)
    response=ask(c,h,regenerate_message_id=first['id'],expected_leaf=first['id'])
    assert '"status": "completed"' in response.text
    assert len(rpc.inputs)==count+2
    assert len(store.history(P1))==2 and len(store.history(P1,all_versions=True))==3


def test_legacy_flat_history_migrates_once_without_losing_messages(tmp_path):
    runtime=tmp_path/'runtime';runtime.mkdir()
    db=sqlite3.connect(runtime/'conversations.sqlite3')
    db.executescript('CREATE TABLE papers (id TEXT PRIMARY KEY,thread TEXT,document TEXT);'
                    'CREATE TABLE messages (id INTEGER PRIMARY KEY,paper TEXT NOT NULL,role TEXT NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL);')
    db.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?)',[(1,P1,'user','old question','completed',1),(2,P1,'assistant','old answer','completed',2),(3,P2,'user','other paper','completed',3)])
    db.commit();db.close()
    store=Store(runtime,tmp_path)
    assert [r['content'] for r in store.history(P1)]==['old question','old answer']
    assert store.history(P1)[1]['parent_id']==1
    again=Store(runtime,tmp_path)
    assert again.history(P1)==store.history(P1)
    assert again.history(P2)[0]['content']=='other paper'
