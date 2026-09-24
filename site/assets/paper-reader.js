const COLORS = {yellow:'#ffd137', blue:'#237bdf', red:'#db3029', green:'#239d51'};
const NAMES = {highlight:'高亮', underline:'下划线', strikeout:'删除线', ink:'手绘', line:'直线', note:'笔记'};
const clone = value => structuredClone(value);
const bounded = n => Math.max(0, Math.min(1, n));

export function createReader({dialog, assets, api, onSelection, onDocument, onLayout}) {
  const root = document.createElement('section'); root.className = 'reader-pane'; root.setAttribute('aria-label', '论文 PDF 阅读器');
  root.innerHTML = `<header class="reader-header"><div class="reader-title-row"><strong>论文阅读</strong><span class="reader-name"></span><button type="button" data-read="hide" aria-label="收起原文阅读区">收起</button></div>
    <div class="reader-version-row"><label>PDF 版本 <select class="reader-version" aria-label="选择 PDF 版本"><option value="">原文 PDF</option></select></label><button type="button" data-read="download" disabled>下载当前 PDF</button></div>
    <div class="reader-tools"><button type="button" data-read="prev" aria-label="上一页">‹</button><label>页 <input class="reader-page-number" type="number" min="1" value="1" aria-label="PDF 页码"></label><span class="reader-page-count">/ 0</span><button type="button" data-read="next" aria-label="下一页">›</button><label class="reader-zoom-label">缩放 <select class="reader-zoom"><option value="fit">适合宽度</option><option value=".75">75%</option><option value="1">100%</option><option value="1.25">125%</option><option value="1.5">150%</option><option value="2">200%</option></select></label><button type="button" data-read="upload">上传 PDF</button><button type="button" data-read="fetch-pdf">获取 PDF</button></div>
    <div class="reader-tools reader-mark-tools"><button type="button" data-tool="select" aria-pressed="true">选择文字</button><button type="button" data-tool="line">画线</button><button type="button" data-tool="ink">画笔</button><button type="button" data-tool="note">笔记</button><label>颜色 <select class="reader-color" aria-label="标记颜色"><option value="yellow">黄色</option><option value="blue">蓝色</option><option value="red">红色</option><option value="green">绿色</option></select></label><button type="button" data-read="undo" disabled>撤销</button><button type="button" data-read="redo" disabled>重做</button><button type="button" data-read="notes" aria-expanded="false">批注 <span class="reader-note-count">0</span></button><button type="button" data-read="export" disabled>导出批注 PDF</button></div>
    <div class="reader-selection" hidden><span class="reader-selection-label"></span><button type="button" data-read="translate">英译中</button><button type="button" data-read="translate-en">中译英</button><button type="button" data-read="question">就此提问</button><button type="button" data-read="highlight">高亮</button><button type="button" data-read="underline">下划线</button><button type="button" data-read="strikeout">删除线</button><button type="button" data-read="dismiss-selection" aria-label="收起划词工具">×</button></div>
    <p class="reader-status" role="status">上传或获取 PDF 后可在这里阅读、标记和划词翻译。</p></header>
    <div class="reader-body"><div class="reader-pages" tabindex="0" aria-label="原文页面"></div><aside class="reader-notes" aria-label="批注与笔记" hidden><div class="reader-notes-heading"><strong>批注与笔记</strong><button type="button" data-read="backup">备份批注</button><button type="button" data-read="reload">重新载入</button></div><div class="reader-note-list"></div></aside></div>`;
  const splitter = document.createElement('div'); splitter.className = 'reader-splitter'; splitter.setAttribute('role','separator'); splitter.setAttribute('aria-label','调整原文与对话宽度'); splitter.setAttribute('aria-orientation','vertical'); splitter.tabIndex = 0;
  const tabs = document.createElement('nav'); tabs.className = 'reader-tabs'; tabs.setAttribute('aria-label','切换阅读区'); tabs.innerHTML='<button type="button" data-tab="pdf">原文</button><button type="button" data-tab="chat" aria-pressed="true">对话</button>';
  dialog.prepend(tabs, root, splitter);
  const $ = s => root.querySelector(s), pages = $('.reader-pages');
  let paper = '', doc = null, pdf = null, task = null, pdfjs = null, loadingLibrary = null, epoch = 0, views = [], current = 1;
  let sourceDoc = null, versions = [], switching = false;
  let items = [], revision = 0, dirty = false, saving = null, saveTimer = null, saveError = false, undo = [], redo = [];
  let selection = null, tool = 'select', observer = null, rendering = false, queue = [], drawing = null, documentLoading = false;
  let visible = true; try { visible = localStorage.getItem('paper-reader-visible') !== 'off'; } catch {}
  const state = (text, error=false) => { $('.reader-status').textContent=text; $('.reader-status').classList.toggle('reader-error', error); };
  const url = suffix => `/api/papers/${paper}${suffix}`;
  const version = () => `?version=${encodeURIComponent(doc?.hash || '')}`;
  const paired = () => doc?.view === 'bilingual';
  const sourcePage = number => paired() ? Math.ceil(number / 2) : number;
  const displayPage = number => paired() ? number * 2 - 1 : number;
  const pageLabel = number => `P${sourcePage(number)}` + (paired() ? (number % 2 ? ' · 原文' : ' · 译文') : doc?.view === 'translated' ? ' · 译文' : '');
  function versionMenu() {
    const select=$('.reader-version');select.replaceChildren();
    for(const item of versions){const option=document.createElement('option');option.value=item.hash;option.textContent=item.label || item.name;
      if(item.message_id && versions.length>3)option.textContent+=` · ${new Date(item.created*1000).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})} · #${item.message_id}`;
      select.append(option);}
    if(!versions.length){const option=document.createElement('option');option.value='';option.textContent='原文 PDF';select.append(option);}
    for(const [view,label] of [['translated','译文 PDF（翻译后可选）'],['bilingual','中英对照 PDF（翻译后可选）']]){
      if(!versions.some(v=>v.view===view)){const option=document.createElement('option');option.textContent=label;option.disabled=true;select.append(option);}}
    select.value=doc?.hash || '';select.disabled=documentLoading || switching || !versions.length;
  }
  function setVisible(value, remember=true) {
    visible=value; dialog.classList.toggle('reader-open', value);
    if (remember) { try { localStorage.setItem('paper-reader-visible', value ? 'on' : 'off'); } catch {} }
    onLayout(); if (value && pdf) resize();
  }
  function tab(name) { dialog.dataset.readerTab=name; tabs.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed', String(b.dataset.tab===name))); if(name==='pdf') resize(); onLayout(); }
  dialog.dataset.readerTab='chat';
  tabs.onclick=e=>{if(e.target.dataset.tab)tab(e.target.dataset.tab);};
  function download(blob, name) { const link=document.createElement('a'), address=URL.createObjectURL(blob); link.href=address;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(address),30000); }
  function controls() {
    $('[data-read="undo"]').disabled=!undo.length; $('[data-read="redo"]').disabled=!redo.length;
    $('[data-read="export"]').disabled=!pdf || documentLoading;
    $('[data-read="download"]').disabled=!pdf || documentLoading;
    $('.reader-version').disabled=documentLoading || switching || !versions.length;
    $('.reader-note-count').textContent=items.length;
    root.querySelectorAll('[data-tool],.reader-color').forEach(b=>b.disabled=!pdf || documentLoading);
    $('.reader-mark-tools').hidden=Boolean(doc && doc.kind!=='pdf');
  }
  function changed(next, keepUndo=true, redrawNotes=true) {
    if(keepUndo) { undo.push(clone(items)); if(undo.length>40)undo.shift(); redo=[]; }
    items=next;dirty=true;saveError=false;drawAll();if(redrawNotes)renderNotes();controls();state('正在保存批注…');
    clearTimeout(saveTimer);saveTimer=setTimeout(()=>flush().catch(()=>{}),300);
  }
  async function flush() {
    clearTimeout(saveTimer);
    if(saving) return saving;
    if(!dirty) return;
    const targetPaper=paper, hash=doc?.hash;
    saving=(async()=>{
      try {
        while(dirty) {
          const snapshot=clone(items), payload={document_hash:hash,revision,items:snapshot};
          const data=await api(`/api/papers/${targetPaper}/annotations`,{method:'POST',body:JSON.stringify(payload)});
          if(paper!==targetPaper || doc?.hash!==hash)return;
          revision=data.revision;dirty=JSON.stringify(items)!==JSON.stringify(snapshot);saveError=false;
          state(dirty?'正在保存新批注…':'批注已保存到本机');
        }
      } catch(error) {saveError=true;state(error.message+' 未保存的修改仍保留在本页，可备份批注。',true);throw error;}
    })();
    try {await saving;} finally {saving=null;}
  }
  function svg(tag, attrs) {const el=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const[k,v]of Object.entries(attrs))el.setAttribute(k,String(v));return el;}
  function draw(view, preview=null) {
    const layer=view.overlay;layer.replaceChildren();
    for(const item of [...items.filter(a=>a.page===view.number),...(preview?[preview]:[])]) {
      const group=svg('g',{'data-annotation':item.id || '',stroke:COLORS[item.color],fill:'none'});
      if(item.kind==='highlight')for(const r of item.rects)group.append(svg('rect',{x:r[0]*1000,y:r[1]*1000,width:(r[2]-r[0])*1000,height:(r[3]-r[1])*1000,fill:COLORS[item.color],stroke:'none',opacity:.32}));
      else if(['underline','strikeout'].includes(item.kind))for(const r of item.rects){const y=item.kind==='underline'?r[3]:(r[1]+r[3])/2;group.append(svg('line',{x1:r[0]*1000,x2:r[2]*1000,y1:y*1000,y2:y*1000,'stroke-width':2,'vector-effect':'non-scaling-stroke'}));}
      else if(['line','ink'].includes(item.kind))group.append(svg('polyline',{points:item.points.map(p=>p.map(n=>n*1000).join(',')).join(' '),'stroke-width':2,'vector-effect':'non-scaling-stroke','stroke-linecap':'round','stroke-linejoin':'round'}));
      else {const p=item.points[0];group.append(svg('circle',{cx:p[0]*1000,cy:p[1]*1000,r:9,fill:COLORS[item.color],stroke:'#fff','stroke-width':1}));}
      layer.append(group);
    }
  }
  const drawAll=()=>views.forEach(v=>draw(v));
  function renderNotes() {
    const list=$('.reader-note-list');list.replaceChildren();
    if(!items.length){const p=document.createElement('p');p.textContent='选中文字可高亮、划线；选择“笔记”后点击页面添加。';list.append(p);return;}
    for(const item of items){
      const card=document.createElement('article');card.className='reader-note';card.dataset.annotation=item.id;
      const jump=document.createElement('button');jump.type='button';jump.textContent=`${pageLabel(item.page)} · ${NAMES[item.kind]}`;jump.onclick=()=>go(item.page);
      const quote=document.createElement('p');quote.textContent=item.text.slice(0,250);quote.hidden=!item.text;
      const input=document.createElement('textarea');input.value=item.note;input.rows=2;input.placeholder='添加笔记…';input.maxLength=4000;input.setAttribute('aria-label',`第 ${item.page} 页批注`);
      input.oninput=()=>changed(items.map(a=>a.id===item.id?{...a,note:input.value}:a),true,false);
      const color=document.createElement('select');color.setAttribute('aria-label','修改批注颜色');for(const option of $('.reader-color').options)color.append(option.cloneNode(true));color.value=item.color;color.onchange=()=>changed(items.map(a=>a.id===item.id?{...a,color:color.value}:a));
      const remove=document.createElement('button');remove.type='button';remove.textContent='删除';remove.onclick=()=>changed(items.filter(a=>a.id!==item.id));
      const buttons=document.createElement('div');buttons.append(color,remove);card.append(jump,quote,input,buttons);list.append(card);
    }
  }
  function showNotes(show=true) { $('.reader-notes').hidden=!show;$('[data-read="notes"]').setAttribute('aria-expanded',String(show)); }
  function add(kind, rects=[], points=[], text='', page=current) {
    if(!pdf || items.length>=500){state('每份 PDF 最多保存 500 条批注。',true);return;}
    const item={id:crypto.randomUUID(),page,kind,color:$('.reader-color').value,rects,points,text:text.slice(0,12000),note:''};
    changed([...items,item]);if(kind==='note'){showNotes();$('.reader-note:last-child textarea')?.focus();}return item;
  }
  function selected() {
    const sel=window.getSelection();if(!sel || sel.isCollapsed || !sel.rangeCount)return;
    const range=sel.getRangeAt(0), element=sel.anchorNode?.parentElement;
    const page=element?.closest('.reader-page');if(!page || !pages.contains(page) || !page.contains(sel.focusNode)){selection=null;$('.reader-selection').hidden=true;return;}
    const text=sel.toString().trim();if(!text)return;
    const bounds=page.getBoundingClientRect(), rects=[];
    for(const r of range.getClientRects()) {
      if(r.width<1 || r.height<1)continue;
      const row=[bounded((r.left-bounds.left)/bounds.width),bounded((r.top-bounds.top)/bounds.height),bounded((r.right-bounds.left)/bounds.width),bounded((r.bottom-bounds.top)/bounds.height)];
      if(row[2]>row[0] && row[3]>row[1] && !rects.some(x=>x.every((n,i)=>Math.abs(n-row[i])<.001)))rects.push(row);
    }
    selection={text,page:Number(page.dataset.page),label:page.dataset.label || `P${page.dataset.page}`,rects};
    $('.reader-selection-label').textContent=`已选 ${text.length} 字 · ${selection.label}`;
    $('.reader-selection').hidden=false;
    for(const kind of ['highlight','underline','strikeout'])$(`[data-read="${kind}"]`).hidden=!pdf;
  }
  pages.addEventListener('mouseup',()=>{if(tool==='select')setTimeout(selected,0);});
  pages.addEventListener('keyup',selected);
  document.addEventListener('selectionchange',()=>{if(dialog.open && !window.getSelection()?.isCollapsed && tool==='select')selected();});
  $('.reader-selection').addEventListener('pointerdown',e=>{if(e.target.closest('button'))e.preventDefault();});
  function setTool(value){tool=value;root.dataset.tool=value;root.querySelectorAll('[data-tool]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.tool===value)));}
  pages.addEventListener('pointerdown',event=>{
    if(!pdf || tool==='select' || event.button!==0)return;
    const target=event.target.closest('.reader-page');if(!target)return;
    event.preventDefault();const view=views[Number(target.dataset.page)-1], box=target.getBoundingClientRect();
    const point=e=>[bounded((e.clientX-box.left)/box.width),bounded((e.clientY-box.top)/box.height)];
    if(tool==='note'){add('note',[],[point(event)],'',view.number);setTool('select');return;}
    drawing={view,point,points:[point(event)],kind:tool,color:$('.reader-color').value,pointer:event.pointerId};target.setPointerCapture(event.pointerId);
  });
  pages.addEventListener('pointermove',event=>{
    if(!drawing || drawing.pointer!==event.pointerId)return;const p=drawing.point(event);
    if(drawing.kind==='line')drawing.points=[drawing.points[0],p];else if(drawing.points.length<2000)drawing.points.push(p);
    draw(drawing.view,{...drawing,rects:[]});
  });
  function endDrawing(event,cancel=false){if(!drawing || drawing.pointer!==event.pointerId)return;const d=drawing;drawing=null;if(!cancel && d.points.length>1)add(d.kind,[],d.points,'',d.view.number);else draw(d.view);}
  pages.addEventListener('pointerup',endDrawing);pages.addEventListener('pointercancel',e=>endDrawing(e,true));
  async function library(){
    if(!loadingLibrary)loadingLibrary=import(new URL('vendor/pdfjs/build/pdf.mjs',assets)).then(module=>{module.GlobalWorkerOptions.workerSrc=new URL('vendor/pdfjs/build/pdf.worker.mjs',assets).href;return module;});
    return loadingLibrary;
  }
  async function render(view, generation=epoch) {
    if(view.rendered || view.pending || !pdf)return;view.pending=true;
    try {
      const page=await pdf.getPage(view.number);if(generation!==epoch)return;
      const initial=page.getViewport({scale:1}), chosen=$('.reader-zoom').value;
      const available=paired()?Math.max(250,(pages.clientWidth-48)/2):pages.clientWidth-32;
      const scale=chosen==='fit'?Math.max(.2,available/initial.width):Number(chosen)*96/72;
      const viewport=page.getViewport({scale});view.viewport=viewport;view.node.style.width=`${viewport.width}px`;view.node.style.height=`${viewport.height}px`;view.node.style.setProperty('--total-scale-factor',scale);
      const ratio=Math.min(window.devicePixelRatio || 1,2), canvas=document.createElement('canvas');canvas.width=Math.ceil(viewport.width*ratio);canvas.height=Math.ceil(viewport.height*ratio);canvas.style.width='100%';canvas.style.height='100%';canvas.setAttribute('aria-hidden','true');
      const text=document.createElement('div');text.className='textLayer';view.node.prepend(canvas,text);view.canvas=canvas;view.text=text;
      view.renderTask=page.render({canvasContext:canvas.getContext('2d'),viewport,transform:ratio===1?null:[ratio,0,0,ratio,0,0]});await view.renderTask.promise;
      if(generation!==epoch)return;
      const content=await page.getTextContent();view.textLayer=new pdfjs.TextLayer({textContentSource:content,container:text,viewport});await view.textLayer.render();
      view.node.classList.toggle('reader-scan',!content.items.some(i=>i.str?.trim()));view.rendered=true;view.node.dataset.ready='true';draw(view);
    } catch(error){if(generation===epoch && error.name!=='RenderingCancelledException')state('此页预览失败：'+error.message,true);}
    finally{view.pending=false;}
  }
  async function drain(){if(rendering)return;rendering=true;try{while(queue.length){const view=queue.shift();if(views.includes(view))await render(view);}}finally{rendering=false;}}
  function clearView(v){v.renderTask?.cancel();v.textLayer?.cancel();v.canvas?.remove();v.text?.remove();if(v.canvas){v.canvas.width=0;v.canvas.height=0;}v.rendered=false;v.pending=false;delete v.node.dataset.ready;}
  function buildPages(){
    observer?.disconnect();views.forEach(clearView);views=[];queue=[];pages.replaceChildren();if(!pdf)return;
    pages.classList.toggle('reader-paired',paired());let spread=null;
    for(let number=1;number<=pdf.numPages;number++){
      const node=document.createElement('div');node.className='reader-page';node.dataset.page=number;node.dataset.label=pageLabel(number);node.setAttribute('aria-label',pageLabel(number));
      const width=Math.max(250,paired()?(pages.clientWidth-48)/2:pages.clientWidth-32);node.style.width=`${width}px`;node.style.height=`${width*1.414}px`;
      const overlay=svg('svg',{viewBox:'0 0 1000 1000',preserveAspectRatio:'none','aria-hidden':'true'});overlay.classList.add('reader-overlay');node.append(overlay);
      if(paired()){
        if(number%2){spread=document.createElement('div');spread.className='reader-spread';pages.append(spread);}
        const sheet=document.createElement('div');sheet.className='reader-sheet';const label=document.createElement('div');label.className='reader-page-label';label.textContent=pageLabel(number);sheet.append(label,node);spread.append(sheet);
      }else pages.append(node);
      views.push({number,node,overlay,rendered:false,pending:false});
    }
    observer=new IntersectionObserver(entries=>{for(const e of entries)if(e.isIntersecting){const v=views[Number(e.target.dataset.page)-1];if(v&&!queue.includes(v))queue.push(v);}drain();},{root:pages,rootMargin:'600px 0px'});
    views.forEach(v=>observer.observe(v.node));drawAll();go(Math.min(current,views.length));
  }
  function go(number){current=Math.max(1,Math.min(Number(number)||1,views.length || 1));const v=views[current-1];if(v){pages.scrollTop+=v.node.getBoundingClientRect().top-pages.getBoundingClientRect().top-(paired()?26:0);queue.unshift(v);drain();}$('.reader-page-number').value=sourcePage(current);}
  let scrollFrame;
  pages.onscroll=()=>{cancelAnimationFrame(scrollFrame);scrollFrame=requestAnimationFrame(()=>{
    if(!views.length)return;const y=pages.getBoundingClientRect().top+Math.min(pages.clientHeight/3,160);let best=views[0];for(const v of views){if(paired()&&v.number%2===0)continue;if(v.node.getBoundingClientRect().top<=y)best=v;else break;}current=best.number;$('.reader-page-number').value=sourcePage(current);
    for(const v of views)if(v.rendered && Math.abs(v.number-current)>5)clearView(v);
  });};
  let resizeTimer,lastWidth=0;
  function resize(){clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(!visible || !pdf || pages.clientWidth<100)return;if($('.reader-zoom').value==='fit' && Math.abs(lastWidth-pages.clientWidth)>2){lastWidth=pages.clientWidth;epoch++;buildPages();}},160);}
  new ResizeObserver(resize).observe(pages);
  $('.reader-page-number').onchange=e=>go(displayPage(Number(e.target.value)));$('.reader-zoom').onchange=()=>{epoch++;buildPages();};
  $('.reader-version').onchange=async e=>{try{await selectVersion(e.target.value);}catch(error){versionMenu();state(error.message,true);}};
  async function selectVersion(hash){
    if(switching)return;switching=true;controls();
    try{await update(paper,sourceDoc,versions,hash);if(doc?.hash===hash){try{localStorage.setItem('paper-reader-version:'+paper,hash);}catch{}}}
    finally{switching=false;controls();}
  }
  async function update(nextPaper,nextSource,nextVersions=null,preferred=''){
    const sameSource=nextPaper===paper && (nextSource?.hash || '')===(sourceDoc?.hash || '');
    const available=nextVersions || (sameSource?versions:[]);
    let chosen=preferred || (sameSource?doc?.hash:'');
    if(!chosen){try{chosen=localStorage.getItem('paper-reader-version:'+nextPaper);}catch{}}
    const nextDoc=available.find(v=>v.hash===chosen) || (nextSource?{...nextSource,view:'original'}:null);
    if(nextPaper===paper && (nextDoc?.hash || '')===(doc?.hash || '')){sourceDoc=nextSource;versions=available;versionMenu();return;}
    if(dirty)await flush();
    const keepPage=sameSource?sourcePage(current):1;
    const generation=++epoch;observer?.disconnect();views.forEach(clearView);views=[];queue=[];task?.destroy();task=null;pdf=null;
    paper=nextPaper;sourceDoc=nextSource;versions=available;doc=nextDoc;items=[];revision=0;undo=[];redo=[];selection=null;documentLoading=false;$('.reader-selection').hidden=true;setTool('select');renderNotes();pages.replaceChildren();current=displayPage(keepPage);versionMenu();controls();
    $('.reader-name').textContent=doc?.name || '尚未载入全文';$('.reader-name').title=doc?.name || '';
    $('.reader-page-count').textContent=`/ ${sourcePage(doc?.page_count || 0)}${paired()?' 对页':''}`;
    if(!doc){state('上传或获取 PDF 后可预览、批注和划词翻译。');const empty=document.createElement('div');empty.className='reader-empty';const heading=document.createElement('h3');heading.textContent='打开原文，边读边讨论';const hint=document.createElement('p');hint.textContent='点击上方“上传 PDF”或“获取 PDF”。原文会显示在这里，选中文字即可翻译、提问或标记。';empty.append(heading,hint);pages.append(empty);return;}
    state('正在载入'+(doc.label || '原文')+'…');documentLoading=true;controls();
    try {
      if(doc.kind!=='pdf'){
        const data=await api(url('/reading-text')+version());if(generation!==epoch)return;
        for(const [index,row]of data.pages.entries()){const section=document.createElement('section');section.className='reader-page reader-web-text';section.dataset.page=index+1;section.dataset.label=row.label;const label=document.createElement('small');label.textContent=row.label;const p=document.createElement('p');p.textContent=row.text;section.append(label,p);pages.append(section);}state('网页正文 · 选中文字可翻译或提问；PDF 支持画线批注。');return;
      }
      const [module,response,annotations]=await Promise.all([library(),api(url('/pdf')+version(),{stream:true}),api(url('/annotations')+version())]);
      if(generation!==epoch)return;pdfjs=module;items=annotations.items;revision=annotations.revision;
      const bytes=new Uint8Array(await response.arrayBuffer());if(generation!==epoch)return;
      task=pdfjs.getDocument({data:bytes,isEvalSupported:false,enableXfa:false,cMapUrl:new URL('vendor/pdfjs/web/cmaps/',assets).href,cMapPacked:true,standardFontDataUrl:new URL('vendor/pdfjs/web/standard_fonts/',assets).href,wasmUrl:new URL('vendor/pdfjs/web/wasm/',assets).href});
      const loaded=await task.promise;if(generation!==epoch){loaded.destroy();return;}pdf=loaded;lastWidth=pages.clientWidth;
      $('.reader-page-count').textContent=`/ ${sourcePage(pdf.numPages)}${paired()?' 对页':''}`;$('.reader-page-number').max=sourcePage(pdf.numPages);buildPages();renderNotes();state(paired()?'左侧原文、右侧译文，每张页保留原尺寸；可横向滚动、选字和批注。':'可选择文字、翻译与标记；当前 PDF 的批注独立保存。');
    }catch(error){if(generation===epoch){state('PDF 加载失败：'+error.message+' 可切换版本重试。',true);doc=null;}}
    finally{if(generation===epoch){documentLoading=false;controls();}}
  }
  root.addEventListener('click',async event=>{
    const b=event.target.closest('button');if(!b || b.disabled)return;
    if(b.dataset.tool){setTool(b.dataset.tool);return;}
    const action=b.dataset.read;if(!action)return;
    try {
      if(action==='hide'){await flush();return setVisible(false);}
      if(action==='dismiss-selection'){selection=null;$('.reader-selection').hidden=true;window.getSelection()?.removeAllRanges();return;}
      if(action==='upload' || action==='fetch-pdf')return onDocument(action);
      if(action==='prev' || action==='next')return go(displayPage(sourcePage(current)+(action==='prev'?-1:1)));
      if(action==='notes')return showNotes($('.reader-notes').hidden);
      if(action==='backup')return download(new Blob([JSON.stringify({document_hash:doc?.hash,items},null,2)],{type:'application/json'}),'paper-annotations.json');
      if(action==='reload'){if(dirty&&!confirm('本页有未保存的修改。请先备份批注；继续将载入本机已保存的版本。'))return;dirty=false;const hash=doc?.hash;doc=null;return update(paper,sourceDoc,versions,hash);}
      if(action==='undo' || action==='redo'){const from=action==='undo'?undo:redo,to=action==='undo'?redo:undo;if(from.length){to.push(clone(items));changed(from.pop(),false);}return;}
      if(action==='export'){b.disabled=true;await flush();const response=await api(url('/annotated-pdf')+version(),{stream:true});download(await response.blob(),(doc.name || '论文').replace(/\.pdf$/i,'')+'-批注.pdf');state('已导出批注副本，原始 PDF 保留。');return;}
      if(action==='download'){b.disabled=true;const response=await api(url('/pdf')+version(),{stream:true});download(await response.blob(),(doc.name || '论文').replace(/\.pdf$/i,'')+'.pdf');state('已下载当前 PDF。');return;}
      if(!selection){state('请先在原文页内选中文字。');return;}
      if(['highlight','underline','strikeout'].includes(action)){add(action,selection.rects,[],selection.text,selection.page);window.getSelection()?.removeAllRanges();selection=null;$('.reader-selection').hidden=true;return;}
      if(['translate','translate-en','question'].includes(action)){
        if(selection.text.length>11000){state('选中文字过长，请分段翻译或使用全文翻译。',true);return;}
        const accepted=await onSelection({...selection,action,documentHash:sourceDoc.hash,translated:doc.view==='translated'||(paired()&&selection.page%2===0)});if(accepted){tab('chat');state(action==='question'?'选段已放入右侧提问框，可补充问题后发送。':'已发送选段翻译，请在右侧查看回答和进度。');}
      }
    }catch(error){state(error.message,true);}finally{controls();}
  });
  let drag=null;
  function width(value){const max=Math.max(350,dialog.clientWidth-360);dialog.style.setProperty('--reader-chat-width',`${Math.max(350,Math.min(max,value))}px`);onLayout();}
  try{const saved=Number(localStorage.getItem('paper-reader-chat-width'));if(saved)width(saved);}catch{}
  splitter.onpointerdown=e=>{drag=e.pointerId;splitter.setPointerCapture(e.pointerId);e.preventDefault();};
  splitter.onpointermove=e=>{if(drag===e.pointerId)width(dialog.getBoundingClientRect().right-e.clientX);};
  splitter.onpointerup=()=>{drag=null;try{localStorage.setItem('paper-reader-chat-width',String(dialog.querySelector('.chat-shell').clientWidth));}catch{}};
  splitter.onpointercancel=()=>{drag=null;};splitter.ondblclick=()=>{dialog.style.removeProperty('--reader-chat-width');resize();};
  splitter.onkeydown=e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();width(dialog.querySelector('.chat-shell').clientWidth+(e.key==='ArrowLeft'?40:-40));}};
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
  setVisible(visible,false);controls();
  return {update,flush,open:()=>{setVisible(true);tab('pdf');},
    showPage:async number=>{setVisible(true);tab('pdf');await selectVersion(sourceDoc?.hash);go(number);},
    showTranslation:async (messageId,view='translated')=>{const item=versions.find(v=>(v.message_id===messageId||v.message_ids?.includes(messageId))&&v.view===view);if(!item)return;setVisible(true);tab('pdf');await selectVersion(item.hash);},
    showChat:()=>tab('chat'),discardChanges:()=>{dirty=false;clearTimeout(saveTimer);},hasUnsaved:()=>dirty};
}
