import {prepareTrace, projection, connectionGroups, connectionDirections} from './trace.mjs';
const $ = id => document.getElementById(id);
const ns = 'http://www.w3.org/2000/svg';
let current = null, generation = 0, selectedRecord = null;
function svg(tag, attrs = {}, label = '') {
  const e = document.createElementNS(ns, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
  if (label) e.textContent = label;
  return e;
}
function render() {
  $('plot').replaceChildren(); $('sequence').replaceChildren();
  const plotWidth = Math.max(280, $('plot').clientWidth || 920);
  const plotHeight = Math.max(350, plotWidth * 0.61);
  $('plot').setAttribute('viewBox', `0 0 ${plotWidth} ${plotHeight}`);
  $('show-all').disabled = selectedRecord === null;
  if (!current) {
    $('identity').textContent = '尚未读取结果'; $('counts').textContent = '0 条经过 · 0 段连接'; $('frame').textContent = '';
    $('plot').append(svg('text', {x:plotWidth/2,y:plotHeight/2,'text-anchor':'middle',fill:'#899ca5','font-size':14}, '读取查询结果以查看点位连接'));
    $('geometry-summary').textContent = '读取结果后显示连接记录编号。'; $('selection').textContent = '';
    return;
  }
  const {trace, observations, lines, drawable} = current;
  $('identity').textContent = `${current.activityId ? `活动 ${current.activityId} · ` : ''}${trace.target_id} · 版本 ${trace.version}`;
  $('counts').textContent = `${observations.length} 条经过 · ${drawable.length} 条可绘制记录`;
  $('frame').textContent = `坐标系：${trace.coordinate_frame} · 结果状态：${current.freshness}`;
  const points = observations.filter(o => o.position_status === 'BOUND_MARKER' && o.position_candidates.length === 1);
  const locate = projection(points.map(o => o.position_candidates[0].local_xyz), $('axes').value.split(',').map(Number), plotWidth, plotHeight);
  const groups = connectionGroups(drawable);
  const chosen = lines.find(line => line.index === selectedRecord);
  const collapsed = groups.filter(group => {
    const [a,b] = group.coordinates.map(locate);
    return Math.hypot(a[0]-b[0],a[1]-b[1]) < 0.01;
  }).length;
  $('geometry-summary').textContent = `${drawable.length} 条可绘制记录 · ${groups.length-collapsed} 条图上线段${collapsed?` · ${collapsed} 组点位在当前投影重合`:''}。相同端点的记录共享线段，编号全部保留。`;
  $('selection').textContent = chosen ? `选中记录 #${chosen.index+1}：${chosen.from_camera_id} → ${chosen.to_camera_id}，${chosen.drawing_status==='DRAWABLE'?(chosen.order_status==='ORDERED'?'顺序确定':'空心箭头按记录顺序指向，实际先后待核实'):'此记录没有可绘制端点'}。` : '箭头表示连接记录方向。空心箭头的实际先后待核实；点击编号可单独查看。';
  const defs = svg('defs');
  for (const [id,color,filled] of [['arrow-known','#247e8d',true],['arrow-known-selected','#2375bb',true],
      ['arrow-uncertain','#c48b36',false],['arrow-uncertain-selected','#2375bb',false]]) {
    const arrow=svg('marker',{id,viewBox:'0 0 12 12',refX:18,refY:6,markerWidth:12,markerHeight:12,
      markerUnits:'userSpaceOnUse',orient:'auto-start-reverse','data-certainty':filled?'ORDERED':'UNCERTAIN_CLOCK_ORDER'});
    arrow.append(svg('path',{d:'M 1 1 L 11 6 L 1 11 Z',fill:filled?color:'white',stroke:color,'stroke-width':1.5}));
    defs.append(arrow);
  }
  $('plot').append(defs);
  const choose = index => {
    selectedRecord = selectedRecord === index ? null : index;
    render();
    if (selectedRecord !== null) document.querySelector(`button[data-record-index="${selectedRecord}"]`)?.focus({preventScroll:true});
  };
  for (const group of groups) {
    const [a,b] = group.coordinates.map(locate);
    const directions = connectionDirections(group,selectedRecord);
    const selected = directions.selected;
    const displayed = directions.records;
    const uncertain = displayed.some(record => record.order_status !== 'ORDERED');
    const attrs = {x1:a[0],y1:a[1],x2:b[0],y2:b[1],stroke:selected?'#2375bb':uncertain?'#c48b36':'#247e8d',
      'stroke-width':selected?4:2.5,opacity:chosen&&!selected?0.3:1,
      'data-record-indices':JSON.stringify(group.records.map(record=>record.index))};
    if (selected) attrs['data-selected-record'] = selected.index;
    if (uncertain) attrs['stroke-dasharray'] = '8 6';
    const marker = status => `url(#arrow-${status==='ORDERED'?'known':'uncertain'}${selected?'-selected':''})`;
    if (directions.forward) {
      attrs['marker-end']=marker(directions.forward);attrs['data-forward-order']=directions.forward;
    }
    if (directions.reverse) {
      attrs['marker-start']=marker(directions.reverse);attrs['data-reverse-order']=directions.reverse;
    }
    const element = svg('line', attrs);
    element.append(svg('title', {}, displayed.map(record=>`#${record.index+1} ${record.from_camera_id} → ${record.to_camera_id} · ${record.order_status==='ORDERED'?'顺序确定':'仅记录方向，实际先后待核实'}`).join('\n')));
    $('plot').append(element);
    const dx=b[0]-a[0],dy=b[1]-a[1],length=Math.hypot(dx,dy);
    const x=(a[0]+b[0])/2+(length?-dy/length*18:0), y=(a[1]+b[1])/2+(length?dx/length*18:-20);
    const numbers=displayed.length<=3?displayed.map(record=>`#${record.index+1}`).join(' / '):`#${displayed[0].index+1}… ${displayed.length}条`;
    const badge=svg('g',{'class':'connection-badge','data-record-indices':JSON.stringify(group.records.map(record=>record.index)),
      role:'button',tabindex:0,'aria-label':`线段连接记录 ${group.records.map(record=>record.index+1).join('、')}`});
    const labelWidth=Math.max(28,numbers.length*7+14);
    badge.append(svg('rect',{x:x-labelWidth/2,y:y-12,width:labelWidth,height:24,rx:6,fill:'white',stroke:selected?'#2375bb':'#d5e0e5'}));
    badge.append(svg('text',{x,y:y+4,'text-anchor':'middle','font-size':12,fill:selected?'#2375bb':'#465f6d'},numbers));
    const cycle=()=>{
      const previous=group.records.findIndex(record=>record.index===selectedRecord);
      choose(group.records[(previous+1)%group.records.length].index);
    };
    badge.addEventListener('click',cycle);
    badge.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();cycle();}});
    $('plot').append(badge);
  }
  const cameras = new Map(points.map(o => [o.camera_id,o]));
  for (const [camera,o] of cameras) {
    const [x,y] = locate(o.position_candidates[0].local_xyz);
    $('plot').append(svg('circle',{cx:x,cy:y,r:6,fill:'#176e79',stroke:'white','stroke-width':2}));
    $('plot').append(svg('text',{x:x+10,y:y-12,fill:'#264a5a','font-size':13},camera));
  }
  if (!observations.length) $('plot').append(svg('text',{x:plotWidth/2,y:plotHeight/2,'text-anchor':'middle',fill:'#899ca5'},'当前结果没有确认经过点位'));
  const reasons = {DRAWABLE:'可绘制',UNRESOLVED_POSITION:'位置缺失或冲突，未连线',SESSION_BREAK:'不同轨迹会话，保留断点'};
  if (!lines.length) {
    const p = document.createElement('p'); p.className='empty'; p.textContent=observations.length?'尚无跨摄像头连接。':'没有确认经过记录。'; $('sequence').append(p);
  }
  for (const line of lines) {
    const row=document.createElement('button'); row.type='button';row.className='row'+(line.drawing_status!=='DRAWABLE'||line.order_status!=='ORDERED'?' warn':'')+(line.index===selectedRecord?' selected':'');
    row.dataset.recordIndex=line.index;row.setAttribute('aria-pressed',String(line.index===selectedRecord));
    row.setAttribute('aria-label',`连接记录 ${line.index+1} ${line.from_camera_id} 到 ${line.to_camera_id}`);
    row.addEventListener('click',()=>choose(line.index));
    const title=document.createElement('strong'); title.textContent=`#${line.index+1} ${line.from_camera_id} → ${line.to_camera_id}`;
    const detail=document.createElement('small'); detail.textContent=`${reasons[line.drawing_status]} · ${line.order_status==='ORDERED'?'顺序确定':'顺序待定'}`;
    const time=document.createElement('small'); time.textContent=`名义记录时间：${new Date(line.from.captured_at_unix_ms).toISOString()} / ${new Date(line.to.captured_at_unix_ms).toISOString()}`;
    row.append(title,detail,time); $('sequence').append(row);
  }
}
$('file').addEventListener('change', async event => {
  const ticket = ++generation, file = event.target.files[0]; if (!file) return;
  // Switching files clears the previous activity/person before reading.
  current=null; selectedRecord=null; render();
  try {
    if (file.size>16*1024*1024) throw new Error('结果文件超过16MiB，请缩小查询结果');
    const value=prepareTrace(JSON.parse(await file.text())); if(ticket!==generation)return;
    current=value; render(); $('message').className='';
    $('message').textContent=`已读取 ${file.name}。${value.evidenceKind==='SYNTHETIC_INTERFACE_EXAMPLE'?'这是合成接口验证示例。':''}实线和虚线均表示摄像头点位的连接。${value.freshness==='STALE_ASSOCIATION'?'这是归属已变化的历史结果，请重新查询当前版本。':''}`;
  } catch(error) {
    if(ticket!==generation)return; current=null; render(); $('message').className='error'; $('message').textContent=error.message;
  }
});
$('axes').addEventListener('change',render);
window.addEventListener('resize',render);
$('sample').addEventListener('click',async()=>{
  const ticket=++generation;current=null;selectedRecord=null;render();
  try {
    const response=await fetch('sample.json');if(!response.ok)throw new Error('合成示例尚未生成，请先运行接口验证脚本');
    const value=prepareTrace(await response.json());if(ticket!==generation)return;
    current=value;render();$('message').className='';$('message').textContent='合成接口验证示例：来自本地 Go/C++ 的人工构造事件，不代表现场身份或实际通路。';
  }catch(error){if(ticket!==generation)return;current=null;render();$('message').className='error';$('message').textContent=error.message;}
});
$('show-all').addEventListener('click',()=>{selectedRecord=null;render();});
$('clear').addEventListener('click',()=>{++generation;current=null;selectedRecord=null;$('file').value='';$('message').className='';$('message').textContent='已清空结果。';render();});
render();
