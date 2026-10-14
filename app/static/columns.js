let datasetRevision=0,datasetColumns=[];
const statusNames={new:'Новый',in_progress:'В процессе',done:'Готово'};
function makeValueControl(column,value){
  const control=document.createElement(column.data_type==='int'?'input':'select');
  control.dataset.labelId=column.id;control.dataset.dataType=column.data_type;
  if(column.data_type==='int'){control.type='number';control.step='1';control.min='-9007199254740991';control.max='9007199254740991';control.value=value??'';control.placeholder='Целое число'}
  else{const empty=document.createElement('option');empty.value='';empty.textContent='Не заполнено';control.append(empty);
    const choices=column.data_type==='bool'?[true,false]:column.options;
    for(const choice of choices){const option=document.createElement('option');option.value=JSON.stringify(choice);option.textContent=typeof choice==='boolean'?(choice?'Да':'Нет'):choice;control.append(option)}
    control.value=value===undefined||value===null?'':JSON.stringify(value);
  }
  return control;
}
function readValue(control){
  if(control.value==='')return null;
  if(control.dataset.dataType==='int'){const value=Number(control.value);if(!Number.isSafeInteger(value))throw Error('Введите целое число в допустимом диапазоне');return value}
  return JSON.parse(control.value);
}
function readColumnValues(container){const result={};for(const c of container.querySelectorAll('[data-label-id]'))result[c.dataset.labelId]=readValue(c);return result}
function drawColumnInputs(container,stored={}){
  container.replaceChildren();for(const col of datasetColumns.filter(c=>c.active)){const label=document.createElement('label');label.append(document.createTextNode(col.name));const input=makeValueControl(col,stored[col.id]);input.onchange=()=>{dirty=true};input.oninput=()=>{dirty=true};label.append(input);container.append(label)}
}
function displayValue(v){return v===null||v===undefined?'не заполнено':typeof v==='boolean'?(v?'Да':'Нет'):String(v)}
function updateDocumentStatus(){if(current)$('#document-status').value=current.workflow_status||'new'}
