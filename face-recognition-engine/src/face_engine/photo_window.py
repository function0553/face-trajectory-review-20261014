"""Camera-local best-shot selection before recognition and before delivery.

Appearance is used only to avoid near-duplicate feature extraction. It never
assigns a registered identity or copies an embedding onto a different photo.
"""
import numpy as np
import cv2
from face_engine.main_photo import _iou, photo_rank, photo_is_clear


def recognition_candidates(results, aligned, targets):
    """Choose original aligned crops from conservative, short appearance tracks.

    Track anchors refresh after two seconds; a new/ambiguous face always gets
    its own embedding. Bounds and activity/camera/stream isolation are explicit.
    """
    positions = {id(o): i for i, o in enumerate(targets)}
    tracks = []
    for fi, result in enumerate(results):
        info = result.get('frame_info', {})
        scope = (info.get('scope'), info.get('stream_kind'), tuple(info.get('dimensions', [])))
        stamp = info.get('received_at_unix_ms')
        edges, entries = [], []
        for oi, obs in enumerate(result['observations']):
            index = positions[id(obs)]
            small = cv2.resize(aligned[index], (16, 16), interpolation=cv2.INTER_AREA).astype(np.float32)
            entry = dict(index=index, fi=fi, observation=obs, scope=scope, stamp=stamp,
                         box=result.get('observation_boxes', {}).get(obs['observation_id']), signature=small)
            entries.append(entry)
            if type(stamp) is not int or scope[0] is None or entry['box'] is None:
                continue
            for ti, track in enumerate(tracks):
                anchor, previous = track[0], track[-1]
                if (previous['fi'] >= fi or previous['scope'] != scope or
                        type(previous['stamp']) is not int or
                        not 0 < stamp - previous['stamp'] <= 1200 or
                        not 0 < stamp - anchor['stamp'] <= 2000 or
                        _iou(entry['box'], previous['box']) < .65):
                    continue
                a, b = entry['box'], previous['box']
                ratio = min(a[2] - a[0], a[3] - a[1]) / max(1e-9, min(b[2] - b[0], b[3] - b[1]))
                if not .85 <= ratio <= 1.18:
                    continue
                # Compare the anchor too: a gradually changing passerby must
                # not inherit a continuously drifting appearance track.
                if (np.abs(small - previous['signature']).mean() <= 5 and
                        np.abs(small - anchor['signature']).mean() <= 5):
                    edges.append((oi, ti))
        assigned = set()
        for oi, ti in edges:
            if sum(x == oi for x, _ in edges) == 1 and sum(y == ti for _, y in edges) == 1:
                tracks[ti].append(entries[oi]); assigned.add(oi)
        tracks.extend([[entry] for oi, entry in enumerate(entries) if oi not in assigned])
    winners = [max(track, key=lambda e: photo_rank(e['observation'])) for track in tracks]
    keep = {e['index'] for e in winners}
    for result in results:
        all_obs = result['observations']
        result['recognition_candidates'] = len(all_obs)
        result['observations'] = [o for o in all_obs if positions[id(o)] in keep]
        result['repeated_embeddings_avoided'] = len(all_obs) - len(result['observations'])
    return sorted(keep)


def select_window(results):
    """Select whole recognized originals, normalized across main/sub resolution."""
    main_required = prefer_main_evidence(results)
    tracks = []
    for fi, result in enumerate(results):
        info = result.get('frame_info', {})
        width, height = info.get('dimensions', [1, 1])
        stamp, scope = info.get('received_at_unix_ms'), info.get('scope')
        entries, edges = [], []
        for oi, obs in enumerate(result['observations']):
            box = result.get('observation_boxes', {}).get(obs['observation_id'])
            normalized = None if box is None else np.asarray(box) / [width, height, width, height]
            entry = dict(fi=fi, observation=obs, box=normalized, stamp=stamp, scope=scope)
            entries.append(entry)
            for ti, track in enumerate(tracks):
                previous = track[-1]
                if (box is None or previous['box'] is None or scope is None or scope != previous['scope'] or
                        previous['fi'] >= fi or type(stamp) is not int or type(previous['stamp']) is not int or
                        not 0 <= stamp - previous['stamp'] <= 2500 or _iou(normalized, previous['box']) < .15):
                    continue
                a, b = np.asarray(obs['embedding']), np.asarray(previous['observation']['embedding'])
                cosine = float(a @ b / max(1e-9, np.linalg.norm(a) * np.linalg.norm(b)))
                if np.isfinite(cosine) and cosine >= .55:
                    edges.append((oi, ti))
        assigned = set()
        for oi, ti in edges:
            if sum(x == oi for x, _ in edges) == 1 and sum(y == ti for _, y in edges) == 1:
                tracks[ti].append(entries[oi]); assigned.add(oi)
        tracks.extend([[entry] for oi, entry in enumerate(entries) if oi not in assigned])
    selected = []
    for track in tracks:
        require_main = any(e['observation']['observation_id'] in main_required for e in track)
        if require_main:
            # Propagate the paired main requirement along this low-res track;
            # don't let an earlier low-res member evade a later contradiction.
            track = [e for e in track if results[e['fi']].get('frame_info', {}).get('stream_kind') == 'main']
        track = [e for e in track if photo_is_clear(e['observation'])]
        if track:
            # Within an unambiguous same-face track prefer a qualified main
            # original. A sub-stream cannot replace contradictory main evidence.
            mains = [e for e in track if results[e['fi']].get('frame_info', {}).get('stream_kind') == 'main']
            if mains:
                track = mains
            selected.append(max(track, key=lambda e: photo_rank(e['observation'])))
    # P2 retains one person/camera report per activity. Put the best original into its
    # outbox first, before lower quality independent observations can win it.
    selected.sort(key=lambda e: photo_rank(e['observation']), reverse=True)
    return [(e['fi'], e['observation']) for e in selected]


def prefer_main_evidence(results):
    """Do not let a low-res guess override a corresponding usable main face.

    A contradiction need not have similar embeddings. Correspondence here is
    deliberately local: same committed scope, <=2.5s original receipt separation
    and >=0.5 normalized box overlap. Uncertain crossings suppress the low-res
    guess too; every independent main face still goes to normal gallery checks.
    This is not a claim that receipt time is verified camera exposure time.
    """
    mains = []
    suppressed_ids = set()
    for result in results:
        info = result.get('frame_info', {})
        if info.get('stream_kind') != 'main':
            continue
        w, h = info.get('dimensions', [1, 1])
        for obs in result['observations']:
            box = result.get('observation_boxes', {}).get(obs['observation_id'])
            if box is not None:
                mains.append((info, np.asarray(box) / [w, h, w, h], obs))
    for result in results:
        info = result.get('frame_info', {})
        if info.get('stream_kind') != 'sub':
            continue
        w, h = info.get('dimensions', [1, 1])
        suppressed = []
        for low in result['observations']:
            box = result.get('observation_boxes', {}).get(low['observation_id'])
            matches = []
            for main_info, main_box, main in mains:
                lt, mt = info.get('received_at_unix_ms'), main_info.get('received_at_unix_ms')
                if (box is None or info.get('scope') is None or info['scope'] != main_info.get('scope')
                        or type(lt) is not int or type(mt) is not int or abs(lt - mt) > 2500
                        or _iou(np.asarray(box) / [w, h, w, h], main_box) < .5):
                    continue
                lq, mq = low.get('quality', {}).get('metrics', {}), main.get('quality', {}).get('metrics', {})
                better = (photo_rank(main)[0] or
                          (mq.get('face_size_px', 0) >= 1.5 * max(1, lq.get('face_size_px', 0))
                           and mq.get('laplacian_variance', 0) >= .75 * lq.get('laplacian_variance', 0)))
                if better:
                    matches.append(main['observation_id'])
            if matches:
                suppressed_ids.add(low['observation_id'])
                suppressed.append(dict(observation_id=low['observation_id'],
                    main_observation_ids=matches, reason='MAIN_FACE_EVIDENCE_REQUIRED'))
        result['low_resolution_identity_suppressed'] = suppressed
    return suppressed_ids
