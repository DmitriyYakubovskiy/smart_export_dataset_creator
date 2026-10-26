let datasetRevision=0,datasetColumns=[];
const statusNames={new:'Новый',in_progress:'В процессе',done:'Готово'};
function makeValueControl(column,value,state=value===undefined?'unreviewed':value===null?'no_data':'value'){
  const control=document.createElement(column.data_type==='int'?'div':'select');
  control.dataset.labelId=column.id;control.dataset.dataType=column.data_type;
  const select=column.data_type==='int'?document.createElement('select'):control;
  for(const [v,title] of [['','Не проверено'],['null','Нет данных']]){const option=document.createElement('option');option.value=v;option.textContent=title;select.append(option)}
  if(column.data_type==='int'){
    const option=document.createElement('option');option.value='value';option.textContent='Указать число';select.append(option);
    const input=document.createElement('input');input.type='number';input.step='1';input.min='-9007199254740991';input.max='9007199254740991';input.value=value??'';input.placeholder='Целое число';input.setAttribute('aria-label',column.name+' — целое число');
    select.value=state==='value'?'value':state==='no_data'?'null':'';
    const update=()=>{input.hidden=select.value!=='value';input.disabled=input.hidden};select.onchange=update;update();control.append(select,input);
  }else{
    const choices=column.data_type==='bool'?[true,false]:column.options;
    for(const choice of choices){const option=document.createElement('option');option.value=JSON.stringify(choice);option.textContent=typeof choice==='boolean'?(choice?'Да':'Нет'):choice;select.append(option)}
    select.value=state==='unreviewed'?'':state==='no_data'?'null':JSON.stringify(value);
  }
  select.setAttribute('aria-label',column.name+' — значение или состояние');
  return control;
}
function readValueState(control){
  const select=control.dataset.dataType==='int'?control.querySelector('select'):control;
  return select.value===''?'unreviewed':select.value==='null'?'no_data':'value';
}
function readValue(control){
  if(readValueState(control)!=='value')return null;
  if(control.dataset.dataType==='int'){const raw=control.querySelector('input').value;if(raw==='')throw Error('Введите целое число или выберите «Нет данных» / «Не проверено»');const value=Number(raw);if(!Number.isSafeInteger(value))throw Error('Введите целое число в допустимом диапазоне');return value}
  return JSON.parse(control.value);
}
function readColumnValues(container){const result={};for(const c of container.querySelectorAll('[data-label-id]'))result[c.dataset.labelId]=readValue(c);return result}
function readColumnStates(container){const result={};for(const c of container.querySelectorAll('[data-label-id]'))result[c.dataset.labelId]=readValueState(c);return result}
function drawColumnInputs(container,stored={},states={}){
  container.replaceChildren();for(const col of datasetColumns.filter(c=>c.active)){const label=document.createElement('label');label.append(document.createTextNode(col.name));const input=makeValueControl(col,stored[col.id],states[col.id]);input.addEventListener('change',()=>{dirty=true});input.addEventListener('input',()=>{dirty=true});label.append(input);container.append(label)}
}
function displayValue(v,state=v===undefined?'unreviewed':v===null?'no_data':'value'){return state==='unreviewed'?'не проверено':state==='no_data'?'нет данных':typeof v==='boolean'?(v?'Да':'Нет'):String(v)}
function updateDocumentStatus(){if(current)$('#document-status').value=current.workflow_status||'new'}
