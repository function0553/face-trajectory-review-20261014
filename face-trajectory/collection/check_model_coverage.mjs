// Offline model diagnostic, reusing the established Recast/Detour audit algorithm.
// Camera markers are not person positions; no capture sequence or target route is supplied.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';

const usage = `Usage: node collection/check_model_coverage.mjs
  --scene <site-scene.json> --navmesh <navmesh.bin>
  --geometry-report <geometry-report.json> --output <new-report.json>
  [--recast-module <existing @recast-navigation/core/dist/index.mjs>]

Reads local files only; never installs packages or overwrites an output.
Limits: scene 16 MiB, geometry report 64 MiB, navmesh 256 MiB,
1024 camera markers, 100000 ordered pairs, output 64 MiB.
Scenes with model_revision require the same nonempty revision string in the
geometry report. Matching declarations do not verify the model build chain.
Optional geometry source_scene_sha256/source_navmesh_sha256 must match inputs.`;
const limits = { scene: 16 * 1024 ** 2, geometry: 64 * 1024 ** 2,
  navmesh: 256 * 1024 ** 2, markers: 1024, pairs: 100000, output: 64 * 1024 ** 2 };
const sha = b => crypto.createHash('sha256').update(b).digest('hex');
const vector = p => Array.isArray(p) && p.length === 3 && p.every(Number.isFinite);
const navPoint = p => ({ x: p[0], y: p[2], z: -p[1] });
const localPoint = p => [p.x, -p.z, p.y];
const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);

function parseArgs(args) {
  const allowed = ['scene', 'navmesh', 'geometry-report', 'output', 'recast-module'];
  const options = {};
  for (let i = 0; i < args.length; i += 2) {
    const key = args[i].replace(/^--/, '');
    if (!args[i].startsWith('--') || !allowed.includes(key) || key in options
        || !args[i + 1] || args[i + 1].startsWith('--')) {
      throw Error(`Invalid or duplicate argument: ${args[i]}. Use --help.`);
    }
    options[key] = path.resolve(args[i + 1]);
  }
  for (const key of allowed.slice(0, 4)) {
    if (!options[key]) throw Error(`Required argument: --${key}. Use --help.`);
  }
  return options;
}

function readBounded(filename, maxBytes) {
  const stat = fs.statSync(filename);
  if (!stat.isFile() || stat.size > maxBytes) throw Error(`Input is not a file within ${maxBytes} bytes: ${filename}`);
  return fs.readFileSync(filename);
}

function revision(object, name) {
  if (!Object.hasOwn(object, 'model_revision')) return null;
  if (typeof object.model_revision !== 'string' || !object.model_revision.trim()) {
    throw Error(`${name}.model_revision must be a nonempty string when present`);
  }
  return object.model_revision;
}

function checkHash(source, field, expected) {
  if (!Object.hasOwn(source, field)) return 'NOT_RECORDED';
  if (typeof source[field] !== 'string' || !/^[a-f0-9]{64}$/i.test(source[field])
      || source[field].toLowerCase() !== expected) {
    throw Error(`geometry-report.${field} does not match the supplied input`);
  }
  return 'MATCHED';
}

async function main() {
  if (process.argv.slice(2).length === 1 && process.argv[2] === '--help') {
    console.log(usage);
    return;
  }
  const options = parseArgs(process.argv.slice(2));
  if (fs.existsSync(options.output)) throw Error(`Refusing to overwrite output: ${options.output}`);
  if (!fs.statSync(path.dirname(options.output)).isDirectory()) throw Error('Output parent must be an existing directory');
  const sceneBytes = readBounded(options.scene, limits.scene);
  const navBytes = readBounded(options.navmesh, limits.navmesh);
  const geometryBytes = readBounded(options['geometry-report'], limits.geometry);
  const scene = JSON.parse(sceneBytes), source = JSON.parse(geometryBytes);
  if (scene.unit !== 'm') throw Error('Only scene.unit=m is supported by this metric audit');
  if (!Array.isArray(scene.annotations)) throw Error('scene.annotations must be an array');
  if (!Array.isArray(source.local_roi) || source.local_roi.length !== 2
      || !source.local_roi.every(vector)
      || source.local_roi[0].some((v, i) => v > source.local_roi[1][i])) {
    throw Error('geometry-report.local_roi must contain ordered finite XYZ bounds');
  }
  const sceneRevision = revision(scene, 'scene');
  const sourceRevision = revision(source, 'geometry-report');
  if (sceneRevision !== null && sceneRevision !== sourceRevision) {
    throw Error('scene.model_revision requires an identical geometry-report.model_revision; regenerate or recover provenance for this model');
  }
  const modelBinding = {};
  for (const key of ['model_id', 'scene_url', 'tileset_id', 'calibration_profile']) {
    if (typeof scene[key] !== 'string' || !scene[key].trim()) throw Error(`scene.${key} must be a nonempty string`);
    modelBinding[key] = scene[key];
  }
  if (sceneRevision !== null) modelBinding.model_revision = sceneRevision;
  const provenance = {
    scene_sha256: sha(sceneBytes), navmesh_sha256: sha(navBytes), geometry_report_sha256: sha(geometryBytes),
    status: sceneRevision === null ? 'LEGACY_UNVERIFIED' : 'REVISION_MATCHED_PROVENANCE_UNVERIFIED',
    model_revision_check: sceneRevision === null ? 'NOT_RECORDED' : 'MATCHED',
    geometry_report_model_revision: sourceRevision,
    geometry_scene_hash_check: checkHash(source, 'source_scene_sha256', sha(sceneBytes)),
    geometry_navmesh_hash_check: checkHash(source, 'source_navmesh_sha256', sha(navBytes)),
    geometry_source_entry: source.source_entry ?? null,
    full_model_source_verified: false, navmesh_generation_chain_verified: false,
    source_geometry_hashes_rechecked: false,
  };
  const cameraAnnotations = scene.annotations.filter(a => a.role === 'CAMERA');
  if (cameraAnnotations.length > limits.markers) throw Error(`Camera marker limit exceeded: ${limits.markers}`);
  const ids = new Set();
  for (const a of cameraAnnotations) {
    if (typeof a.annotation_id !== 'string' || !a.annotation_id || ids.has(a.annotation_id)
        || !Array.isArray(a.points) || !vector(a.points[0])
        || (a.camera_id != null && typeof a.camera_id !== 'string')) {
      throw Error('Camera annotations require unique IDs, finite XYZ points and string camera IDs');
    }
    ids.add(a.annotation_id);
  }
  let moduleUrl;
  try {
    moduleUrl = options['recast-module'] ? pathToFileURL(options['recast-module']).href
      : import.meta.resolve('@recast-navigation/core');
  } catch {
    throw Error('Recast is not installed here; supply --recast-module pointing to an existing local core/dist/index.mjs');
  }
  const moduleBytes = readBounded(fileURLToPath(moduleUrl), limits.geometry);
  const { init, importNavMesh, NavMeshQuery, Detour } = await import(moduleUrl);
  await init();
  const { navMesh } = importNavMesh(new Uint8Array(navBytes));
  let query;
  let report;
  try {
    query = new NavMeshQuery(navMesh, { maxNodes: 32768 });
    const markers = [];
    for (const a of cameraAnnotations) {
      const p = a.points[0];
      const inside = p.every((v, i) => v >= source.local_roi[0][i] && v <= source.local_roi[1][i]);
      const entry = { annotation_id: a.annotation_id, camera_id: a.camera_id || null, name: a.name,
        marker_local_xyz: p, inside_copied_roi: inside, nearby_surface: false,
        person_observation_region_verified: false, projection_is_person_position: false };
      if (inside) {
        const r = query.findNearestPoly(navPoint(p), { halfExtents: { x: 2, y: 2, z: 2 } });
        entry.status = r.status;
        entry.nearby_surface = !!(r.success && r.nearestRef);
        if (entry.nearby_surface) {
          entry.nav_polygon_ref = r.nearestRef;
          entry.surface_candidate_local_xyz = localPoint(r.nearestPoint);
          entry.horizontal_distance_m = Math.hypot(p[0] - r.nearestPoint.x, p[1] + r.nearestPoint.z);
          entry.vertical_difference_m = p[2] - r.nearestPoint.y;
          entry.marker_over_polygon = r.isOverPoly;
        }
      }
      markers.push(entry);
    }
    const bound = markers.filter(m => m.camera_id);
    const countsByDevice = new Map();
    for (const m of bound) countsByDevice.set(m.camera_id, (countsByDevice.get(m.camera_id) || 0) + 1);
    const conflicts = [...countsByDevice.entries()].filter(([, n]) => n > 1).map(([id]) => {
      const candidates = bound.filter(m => m.camera_id === id);
      let maxSeparation = 0;
      for (const a of candidates) for (const b of candidates) {
        maxSeparation = Math.max(maxSeparation, Math.hypot(...a.marker_local_xyz.map((v, i) => v - b.marker_local_xyz[i])));
      }
      return { camera_id: id, annotation_ids: candidates.map(m => m.annotation_id),
        max_separation_m: maxSeparation,
        handling: 'KEEP_ALL_CANDIDATES' };
    });
    const projected = bound.filter(m => m.nearby_surface);
    const pairCount = projected.reduce((sum, a) => sum + projected.filter(b => a.camera_id !== b.camera_id).length, 0);
    if (pairCount > limits.pairs) throw Error(`Ordered pair limit exceeded: ${pairCount} > ${limits.pairs}`);
    const pairs = [];
    for (const a of projected) for (const b of projected) {
      if (a.annotation_id === b.annotation_id || a.camera_id === b.camera_id) continue;
      const start = navPoint(a.surface_candidate_local_xyz), end = navPoint(b.surface_candidate_local_xyz);
      // computePath hides success-detail flags and may clamp partial paths. Inspect native statuses.
      const r = query.findPath(a.nav_polygon_ref, b.nav_polygon_ref, start, end, { maxPathPolys: 4096 });
      const flags = Detour.DT_BUFFER_TOO_SMALL | Detour.DT_OUT_OF_NODES | Detour.DT_PARTIAL_RESULT;
      let reached = !!(r.success && r.polys.size && r.polys.get(r.polys.size - 1) === b.nav_polygon_ref);
      let complete = reached && !(r.status & flags), length = null, straightStatus = null, waypoints = null;
      try {
        if (complete) {
          const straight = query.findStraightPath(start, end, r.polys, { maxStraightPathPoints: 4096 });
          try {
            straightStatus = straight.status;
            const points = Array.from({ length: straight.straightPathCount }, (_, i) => ({
              x: straight.straightPath.get(i * 3), y: straight.straightPath.get(i * 3 + 1), z: straight.straightPath.get(i * 3 + 2) }));
            reached = !!(points.length && distance(points.at(-1), end) <= .2);
            complete = !!(straight.success && reached && !(straight.status & flags));
            if (complete) {
              length = points.slice(1).reduce((sum, p, i) => sum + distance(p, points[i]), 0);
              // Export scene-local coordinates, never raw Detour [x,z,-y].
              waypoints = points.map(p => {const [x,y,z] = localPoint(p); return {x,y,z};});
            }
          } finally {
            straight.straightPath.destroy(); straight.straightPathFlags.destroy(); straight.straightPathRefs.destroy();
          }
        }
      } finally { r.polys.destroy(); }
      pairs.push({ from_annotation_id: a.annotation_id, to_annotation_id: b.annotation_id,
        from_camera_id: a.camera_id, to_camera_id: b.camera_id, complete_model_path: complete,
        reached_destination: reached, path_status: r.status, straight_path_status: straightStatus,
        model_path_length_m: length, ...(waypoints ? {waypoints} : {}), real_pedestrian_route_verified: false });
    }
    report = { schema: 'general-model-suitability-audit@2', production_executed: false,
      trajectory_acceptance: false, camera_fov_verified: false, navigability_field_verified: false,
      source_scene_sha256: provenance.scene_sha256, source_navmesh_sha256: provenance.navmesh_sha256,
      source_geometry_report_sha256: provenance.geometry_report_sha256,
      model_binding: modelBinding, provenance,
      inputs: { scene: options.scene, navmesh: options.navmesh, geometry_report: options['geometry-report'],
        recast_module: fileURLToPath(moduleUrl), recast_module_sha256: sha(moduleBytes),
        audit_script_sha256: sha(fs.readFileSync(fileURLToPath(import.meta.url))) },
      coordinate_mapping: 'scene local [x,y,z] metres -> navmesh [x,z,-y]; inherited from geometry extraction',
      projection_search_half_extents_m: [2, 2, 2], source_roi: source.local_roi,
      summary: { camera_markers: markers.length, bound_markers: bound.length,
        bound_devices: countsByDevice.size, unbound_markers: markers.length - bound.length,
        markers_in_roi: markers.filter(m => m.inside_copied_roi).length,
        bound_markers_in_roi: bound.filter(m => m.inside_copied_roi).length,
        bound_markers_with_nearby_surface: projected.length,
        bound_markers_without_nearby_surface: bound.filter(m => m.inside_copied_roi && !m.nearby_surface).length,
        ordered_pairs_checked: pairs.length, complete_model_paths: pairs.filter(p => p.complete_model_path).length,
        disconnected_partial_or_failed: pairs.filter(p => !p.complete_model_path).length },
      conflicts, markers, pairs,
      limitations: [
        'Nearby navigation polygon is a model diagnostic, not camera FOV or a pedestrian footpoint.',
        'Only the supplied ROI is covered; the source may contain unmeasured or omitted geometry.',
        'Shortest model paths are not actual-person routes, nor a complete alternative-route topology.',
        'No target-specific prepared graph, simulation truth, future observation, or C++ reconstruction is used.',
        'Input hashes identify supplied files only. Model source files, transforms and navmesh generation were not revalidated.',
        'A matching declared revision does not prove that the supplied navmesh was generated from that model.',
      ] };
  } finally {
    query?.destroy();
    navMesh.destroy();
  }
  const output = JSON.stringify(report, null, 2) + '\n';
  if (Buffer.byteLength(output) > limits.output) throw Error(`Report exceeds output limit: ${limits.output}`);
  fs.writeFileSync(options.output, output, { flag: 'wx' });
  console.log(JSON.stringify({ output: options.output, summary: report.summary,
    provenance_status: report.provenance.status, trajectory_acceptance: false }, null, 2));
}

main().catch(error => {
  console.error(`check_model_coverage: ${error.message}`);
  process.exitCode = 1;
});
