/* Offsets are Unicode code points, NOT JavaScript UTF-16 units. */
function codePointLength(text) { return Array.from(text).length; }
function spanSegments(text, annotations) {
  const chars=Array.from(text), bounds=[...new Set([0,chars.length,...annotations.flatMap(a=>[a.start,a.end])])].sort((a,b)=>a-b);
  return bounds.slice(0,-1).map((start,i)=>({start,end:bounds[i+1],text:chars.slice(start,bounds[i+1]).join(''),labels:annotations.filter(a=>a.start<bounds[i+1]&&a.end>start)}));
}
let labelDefs=[],annotationItems=[],annotationCurrent=null,annotationSelection=null,annotationFilter='pending',annotationBusy=false,detailRequest=0;
const guardAnnotation=fn=>async(...args)=>{try{await fn(...args)}catch(e){toast(e.message)}};
async function annotationMode(){
  if(loading||!permit())return false;
  resetForm();$('#document-work').hidden=true;$('#annotation-work').hidden=false;
  $('#mode-documents').classList.remove('active');$('#mode-annotations').classList.add('active');
  await refreshLabels();await refreshAnnotationList();return true;
}
$('#mode-annotations').onclick=guardAnnotation(annotationMode);
$('#mode-documents').onclick=()=>{if(annotationBusy)return;$('#annotation-work').hidden=true;$('#document-work').hidden=false;$('#mode-documents').classList.add('active');$('#mode-annotations').classList.remove('active');if(current)loadHistories().catch(e=>toast(e.message))};
window.openAnnotation=guardAnnotation(async id=>{if(await annotationMode())await selectAnnotation(id)});
async function refreshLabels(){
  const schema=await api('/schema');datasetRevision=schema.revision;datasetColumns=schema.columns;labelDefs=schema.columns;const chosen=$('#span-label').value;$('#span-label').replaceChildren();
  for(const l of labelDefs.filter(l=>l.active)){const option=element('option',`${l.name} · ${l.code}`);option.value=l.id;$('#span-label').append(option)}
  if(labelDefs.some(l=>l.active&&l.id===chosen))$('#span-label').value=chosen;
  drawSpanResult();$('#add-span').disabled=!annotationSelection||!$('#span-label').value||annotationBusy;
}
async function refreshAnnotationList(){
  annotationItems=await api('/histories');drawAnnotationList();
}
function drawAnnotationList(){
  const done=annotationItems.filter(h=>h.completed).length;$('#annotation-total').textContent=annotationItems.length;
  $('#pending-tab').textContent=`Неразмеченные (${annotationItems.length-done})`;$('#completed-tab').textContent=`Размеченные (${done})`;
  $('#annotation-summary').textContent='Размеченные — записи с подтверждённым завершением.';
  const query=$('#annotation-search').value.toLowerCase(),list=$('#annotation-list');list.replaceChildren();
  for(const h of annotationItems.filter(h=>!!h.completed===(annotationFilter==='completed')&&`${h.title} ${h.text} ${h.document_name}`.toLowerCase().includes(query))){
    const b=element('button','',`doc-card ${annotationCurrent?.id===h.id?'active':''}`);
    b.append(element('strong',h.title),element('small',h.document_name),element('p',h.text.slice(0,105)+(h.text.length>105?'…':''),'annotation-snippet'),element('small',`${h.annotation_count} фрагментов · ${statusNames[h.workflow_status]||'Новый'}`));
    b.onclick=guardAnnotation(()=>selectAnnotation(h.id));list.append(b);
  }
  if(!list.children.length)list.append(element('p',annotationItems.length?'В этом списке пока нет анамнезов.':'Сначала сохраните анамнезы в разделе «Документы».','empty-list'));
}
for(const [id,filter] of [['#pending-tab','pending'],['#completed-tab','completed']])$(id).onclick=()=>{annotationFilter=filter;$('#pending-tab').classList.toggle('active',filter==='pending');$('#completed-tab').classList.toggle('active',filter==='completed');drawAnnotationList()};
$('#annotation-search').oninput=drawAnnotationList;
async function selectAnnotation(id){
  if(annotationBusy)return;const request=++detailRequest;await api(`/histories/${id}/open`,{method:'POST'});const item=await api(`/histories/${id}/annotation`);if(request!==detailRequest)return;
  annotationCurrent=item;annotationSelection=null;$('#annotation-empty').hidden=true;$('#annotation-editor').hidden=false;renderAnnotation();drawAnnotationList();
}
function renderAnnotation(){
  const h=annotationCurrent;$('#annotation-title').textContent=h.title;$('#annotation-source').textContent='Источник: '+h.document_name+' · ID анамнеза: '+h.id+' · '+labelDefs.filter(l=>l.active).map(l=>l.name+': '+displayValue(h.values[l.id],h.value_states?.[l.id])).join(' · ');
  $('#annotation-status').textContent=statusNames[h.workflow_status]||'Новый';
  $('#annotation-length').textContent=codePointLength(h.text)+' символов';$('#complete-annotation').textContent=h.completed?'Вернуть в неразмеченные':'Разметка завершена';
  const text=$('#annotation-text');text.replaceChildren();
  for(const segment of spanSegments(h.text,h.annotations)){
    if(!segment.labels.length){text.append(document.createTextNode(segment.text));continue}
    const mark=element('mark',segment.text);mark.style.backgroundColor=segment.labels[0].color;mark.title=segment.labels.map(a=>a.label_name+' · '+a.label).join(', ');text.append(mark);
  }
  $('#span-count').textContent=h.annotations.length;const list=$('#span-list');list.replaceChildren();
  for(const a of h.annotations){const box=element('div','','span-card'),badge=element('span',a.label,'label-chip');badge.style.backgroundColor=a.color;
    box.append(badge,element('p',a.text),element('p','Результат: '+displayValue(a.result,a.result_state),'hint'),element('small',`[${a.start}, ${a.end})`));const del=element('button','Удалить','quiet danger');
    del.onclick=guardAnnotation(()=>mutateAnnotation(()=>api(`/histories/${h.id}/annotations/${a.id}`,{method:'DELETE'}),'Фрагмент удалён'));
    const change=element('button','Изменить результат','quiet');change.onclick=()=>{const col=labelDefs.find(l=>l.id===a.label_id);const control=makeValueControl(col,a.result,a.result_state);const save=element('button','Сохранить результат','secondary');save.onclick=guardAnnotation(()=>{const result=readValue(control),result_state=readValueState(control);return mutateAnnotation(()=>api(`/histories/${h.id}/annotations/${a.id}`,{method:'PATCH',body:JSON.stringify({...a,text_hash:h.text_hash,result,result_state})}),'Результат сохранён')});box.append(control,save)};box.append(change,del);list.append(box);
  }
  if(!h.annotations.length)list.append(element('p','Выделенных фрагментов пока нет.','hint'));
  annotationSelection=null;updateSelectionPanel();
}
function updateSelectionPanel(){
  $('#selected-fragment').textContent=annotationSelection?.text||'Пока ничего не выделено';
  $('#selected-offsets').textContent=annotationSelection?`Начало: ${annotationSelection.start} · Конец: ${annotationSelection.end}`:'';
  $('#add-span').disabled=!annotationSelection||!$('#span-label').value||annotationBusy;
}
function captureAnnotationSelection(){
  const selection=window.getSelection(),container=$('#annotation-text');
  if(!annotationCurrent||!selection.rangeCount||selection.isCollapsed)return;
  const range=selection.getRangeAt(0);
  if(!container.contains(range.startContainer)||!container.contains(range.endContainer))return;
  const prefix=range.cloneRange();prefix.selectNodeContents(container);prefix.setEnd(range.startContainer,range.startOffset);
  const start=codePointLength(prefix.toString()),text=range.toString(),end=start+codePointLength(text);
  if(!text.trim()||Array.from(annotationCurrent.text).slice(start,end).join('')!==text){annotationSelection=null;updateSelectionPanel();return}
  annotationSelection={start,end,text,text_hash:annotationCurrent.text_hash};updateSelectionPanel();
}
$('#annotation-text').addEventListener('mouseup',captureAnnotationSelection);$('#annotation-text').addEventListener('keyup',captureAnnotationSelection);
function drawSpanResult(){const container=$('#span-result');container.replaceChildren();const col=labelDefs.find(l=>l.id===$('#span-label').value);if(col)container.append(makeValueControl(col,undefined))}
$('#span-label').onchange=()=>{drawSpanResult();updateSelectionPanel()};
async function mutateAnnotation(action,message){
  if(annotationBusy||!annotationCurrent)return;annotationBusy=true;const id=annotationCurrent.id;
  $('#annotation-editor').inert=true;
  try{await action();annotationCurrent=await api(`/histories/${id}/annotation`);renderAnnotation();await refreshAnnotationList();toast(message)}
  finally{annotationBusy=false;$('#annotation-editor').inert=false;updateSelectionPanel()}
}
$('#add-span').onclick=guardAnnotation(async()=>{
  if(!annotationSelection)return;const control=$('#span-result').firstElementChild,result=readValue(control),result_state=readValueState(control);if(result_state==='unreviewed')throw Error('Укажите результат фрагмента или выберите «Нет данных»');const selected={...annotationSelection,label_id:$('#span-label').value,result,result_state};
  await mutateAnnotation(()=>api(`/histories/${annotationCurrent.id}/annotations`,{method:'POST',body:JSON.stringify(selected)}),'Метка сохранена');
});
$('#complete-annotation').onclick=guardAnnotation(()=>mutateAnnotation(()=>api(`/histories/${annotationCurrent.id}/annotation`,{method:'PATCH',body:JSON.stringify({completed:!annotationCurrent.completed,text_hash:annotationCurrent.text_hash,schema_revision:annotationCurrent.schema_revision})}),'Статус сохранён'));
$('#reset-annotation').onclick=guardAnnotation(async()=>{
  if(annotationBusy||!confirm('Удалить все метки этого анамнеза? Текст останется, запись вернётся в неразмеченные.'))return;
  await mutateAnnotation(()=>api(`/histories/${annotationCurrent.id}/annotations/reset`,{method:'POST',body:JSON.stringify({text_hash:annotationCurrent.text_hash})}),'Разметка удалена');
});
function drawLabels(){const list=$('#labels-list');list.replaceChildren();for(const l of labelDefs){const row=element('div','','label-row'),info=element('div'),badge=element('span',l.code,'label-chip');badge.style.backgroundColor=l.color;info.append(badge,element('p',l.name+' · '+l.data_type+(l.data_type==='enum'?' · '+l.options.join(', '):'')));const toggle=element('button',l.active?'Отключить':'Включить','quiet');toggle.onclick=guardAnnotation(async()=>{toggle.disabled=true;try{await api('/labels/'+l.id,{method:'PATCH',body:JSON.stringify({name:l.name,color:l.color,active:!l.active})});await refreshWorkflowSchema()}finally{toggle.disabled=false}});row.append(info,toggle);list.append(row)}}
async function refreshWorkflowSchema(){
  const entered=readColumnValues($('#history-columns')),states=readColumnStates($('#history-columns'));
  await refreshLabels();drawLabels();drawColumnInputs($('#history-columns'),entered,states);
  await refresh();if(current){current=docs.find(d=>d.id===current.id);updateDocumentStatus()}
  await refreshAnnotationList();if(annotationCurrent)await selectAnnotation(annotationCurrent.id);
}
async function showLabels(){await refreshLabels();drawLabels();$('#label-error').textContent='';$('#labels-dialog').showModal()}
$('#document-columns').onclick=guardAnnotation(showLabels);$('#manage-labels').onclick=guardAnnotation(showLabels);$('#add-label-shortcut').onclick=guardAnnotation(showLabels);$('#close-labels').onclick=()=>$('#labels-dialog').close();
$('#label-code').oninput=e=>e.target.value=e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g,'');
$('#label-form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button[type=submit]');button.disabled=true;$('#label-error').textContent='';try{await api('/labels',{method:'POST',body:JSON.stringify({code:$('#label-code').value,name:$('#label-name').value,color:$('#label-color').value,data_type:$('#label-type').value,options:$('#label-options').value.split('\n')})});e.target.reset();$('#label-options-wrap').hidden=false;await refreshWorkflowSchema();toast('Колонка добавлена. Готовые записи возвращены в работу')}catch(err){$('#label-error').textContent=err.message}finally{button.disabled=false}};

$('#label-type').onchange=()=>$('#label-options-wrap').hidden=$('#label-type').value!=='enum';
