const fail = message => { throw new Error(message); };
const key = (source, id) => JSON.stringify([source, id]);
const text = (value, name) => typeof value === 'string' && value.length > 0 ? value : fail(`${name} 缺失`);
const point = value => Array.isArray(value) && value.length === 3 && value.every(Number.isFinite);

export function prepareTrace(value) {
  if (!value || typeof value !== 'object' || value.ok === false) fail('需要成功的 point_trace 查询结果');
  const trace = value.point_trace ?? value;
  text(trace.target_id, 'target_id');
  text(trace.coordinate_frame, 'coordinate_frame');
  if (trace.drawing_mode !== 'CAMERA_POINT_CONNECTIONS') fail('结果缺少点位连接合同，请使用新版 worker');
  if (!Number.isSafeInteger(trace.version) || trace.version < 1) fail('轨迹版本无效');
  if (!Array.isArray(trace.observations) || !Array.isArray(trace.connections)) fail('经过点位或连接列表无效');
  if (trace.observations.length > 50000 || trace.connections.length > 50000) fail('单次查看最多 50000 条，请缩小查询结果');
  const observations = new Map();
  for (const o of trace.observations) {
    const id = key(text(o.source_id, 'source_id'), text(o.observation_id, 'observation_id'));
    if (observations.has(id)) fail('存在重复的来源/观测键');
    text(o.camera_id, 'camera_id');
    if (o.status !== 'OBSERVED') fail('查看结果含非确认经过记录');
    if (!Number.isSafeInteger(o.captured_at_unix_ms) || Math.abs(o.captured_at_unix_ms) > 8640000000000000 ||
        !Number.isSafeInteger(o.clock_uncertainty_ms) || o.clock_uncertainty_ms < 0)
      fail('时间超出浏览器可精确显示范围');
    if (!Array.isArray(o.position_candidates) || !o.position_candidates.every(p => point(p.local_xyz))) fail('点位坐标无效');
    observations.set(id, o);
  }
  let routePointCount = 0;
  const lines = trace.connections.map((c, index) => {
    const from = observations.get(key(c.from_source_id, c.from_observation_id));
    const to = observations.get(key(c.to_source_id, c.to_observation_id));
    if (!from || !to || c.from_camera_id !== from.camera_id || c.to_camera_id !== to.camera_id || from.camera_id === to.camera_id)
      fail('连接端点与经过记录不一致');
    if (!['ORDERED', 'UNCERTAIN_CLOCK_ORDER'].includes(c.order_status) ||
        !['DRAWABLE', 'UNRESOLVED_POSITION', 'SESSION_BREAK', 'HOLD', 'UNREACHABLE'].includes(c.drawing_status) ||
        c.physical_path_observed !== false || c.meaning !== 'DIRECT_CAMERA_POINT_CONNECTION') fail('连接语义无效');
    if (!Array.isArray(c.coordinates)) fail('连接坐标无效');
    if (c.drawing_status === 'DRAWABLE') {
      if (c.coordinates.length !== 2 || !c.coordinates.every(point) ||
          from.position_status !== 'BOUND_MARKER' || to.position_status !== 'BOUND_MARKER' ||
          from.position_candidates.length !== 1 || to.position_candidates.length !== 1 ||
          c.coordinates.some((p, i) => p.some((v, axis) => v !== [from, to][i].position_candidates[0].local_xyz[axis])))
        fail('可绘制连接必须使用两个唯一的原始点位');
    } else if (c.coordinates.length !== 0) fail('未确定的连接不能带绘制坐标');
    if (c.geometry_kind !== undefined && c.geometry_kind !== 'CAMERA_CHORD') fail('相机弦线类型无效');
    if (c.order_confirmed !== undefined && c.order_confirmed !== (c.order_status === 'ORDERED')) fail('顺序确认标识冲突');
    if (c.direction_status !== undefined && c.direction_status !== (c.order_status === 'ORDERED' ? 'CONFIRMED' : 'ORDER_UNCERTAIN')) fail('方向状态冲突');
    for (const [field,i] of [['from_position',0],['to_position',1]]) {
      if (c[field] !== undefined && JSON.stringify(c[field]) !== JSON.stringify(c.coordinates[i])) fail('相机端点不能被拓扑折点替换');
    }
    if (c.route_waypoints !== undefined) {
      if (c.drawing_status !== 'DRAWABLE' || c.route_status !== 'AVAILABLE' ||
          c.route_geometry_kind !== 'TOPOLOGY_INFERRED_PATH' ||
          !Array.isArray(c.route_waypoints) || c.route_waypoints.length < 2 || c.route_waypoints.length > 4096 ||
          !c.route_waypoints.every(p => p && ['x','y','z'].every(k => Number.isFinite(p[k])))) fail('拓扑参考折线无效');
      routePointCount += c.route_waypoints.length;
      if (routePointCount > 200000) fail('拓扑折点超过 200000 个，请缩小查询结果');
    }
    return {...c, from, to, index};
  });
  return {trace, activityId: value.activity_id ?? null, evidenceKind: value.evidence_kind ?? null,
    observations: [...observations.values()], lines,
    freshness: trace.freshness?.status ?? value.freshness?.status ?? 'UNSPECIFIED',
    drawable: lines.filter(c => c.drawing_status === 'DRAWABLE')};
}

export function projection(points, axes, width = 920, height = 560) {
  if (!points.length) return () => [width / 2, height / 2];
  let unit = 1;
  for (const p of points) unit = Math.max(unit, Math.abs(p[axes[0]]), Math.abs(p[axes[1]]));
  let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
  for (const p of points) {
    const x=p[axes[0]]/unit,y=p[axes[1]]/unit;
    minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);maxY=Math.max(maxY,y);
  }
  const scale = Math.min((width - 140) / Math.max(maxX - minX, 1e-12), (height - 140) / Math.max(maxY - minY, 1e-12));
  return p => [width / 2 + (p[axes[0]] / unit - (minX / 2 + maxX / 2)) * scale,
    height / 2 - (p[axes[1]] / unit - (minY / 2 + maxY / 2)) * scale];
}

export function connectionGroups(lines) {
  const groups = new Map();
  for (const line of lines) {
    // Include full coordinates and camera IDs: separate camera pairs or
    // positions must not be merged just because a 2D projection overlaps.
    const points = line.display_coordinates ?? line.coordinates;
    const variants = [JSON.stringify([line.from_camera_id,line.to_camera_id,points]),
      JSON.stringify([line.to_camera_id,line.from_camera_id,[...points].reverse()])].sort();
    const groupKey = variants[0];
    const found = groups.get(groupKey);
    if (found) found.records.push(line);
    else groups.set(groupKey, {...line, records:[line]});
  }
  return [...groups.values()];
}

export function displayConnections(prepared, mode = 'CHORD') {
  if (!['CHORD','TOPOLOGY'].includes(mode)) fail('未知几何视图');
  return prepared.drawable.filter(c => c.route_status !== 'UNREACHABLE' &&
      (mode === 'CHORD' || (c.route_status === 'AVAILABLE' && c.route_waypoints)))
    .map(c => ({...c, display_coordinates: mode === 'CHORD' ? c.coordinates : c.route_waypoints.map(p => [p.x,p.y,p.z])}));
}

export function connectionMarkers(group, selectedIndex = null) {
  const d = connectionDirections(group, selectedIndex);
  const uncertain = d.records.some(r => r.order_status !== 'ORDERED');
  return {...d, start: uncertain ? 'UNCERTAIN_CLOCK_ORDER' : d.reverse,
    end: uncertain ? 'UNCERTAIN_CLOCK_ORDER' : d.forward, uncertain};
}

export function connectionDirections(group, selectedIndex = null) {
  const selected = group.records.find(record => record.index === selectedIndex);
  const records = selected ? [selected] : group.records;
  const status = items => items.length ? (items.every(record => record.order_status === 'ORDERED')
    ? 'ORDERED' : 'UNCERTAIN_CLOCK_ORDER') : null;
  return {selected, records,
    forward:status(records.filter(record=>record.from_camera_id===group.from_camera_id)),
    reverse:status(records.filter(record=>record.from_camera_id===group.to_camera_id))};
}
