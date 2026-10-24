"""Platform v4.1 photo enrollment and bounded capture encoding.

No pictures, credentials, or vectors are printed to logs. Enrollment stdout is
an internal JSON pipe consumed by the algorithm daemon, never by the platform.
"""
import base64
import hashlib
import json
import math
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MAX_PHOTO_BYTES = 5 * 1024 * 1024


def read_photo(url):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) <= 32 for c in url):
        raise ValueError('personPhoto必须是完整HTTP(S) URL')
    address = urlsplit(url)
    if address.scheme not in ('http', 'https') or not address.hostname or address.username or address.password or address.fragment:
        raise ValueError('personPhoto必须是完整HTTP(S) URL')
    # Platform file servers can be on the private LAN; do not restrict them to public DNS.
    with urlopen(Request(url, headers={'Accept': 'image/jpeg, image/png'}), timeout=15) as reply:
        if urlsplit(reply.geturl()).scheme not in ('http', 'https'):
            raise ValueError('登记照片重定向地址无效')
        raw = reply.read(MAX_PHOTO_BYTES + 1)
    if not raw or len(raw) > MAX_PHOTO_BYTES:
        raise ValueError('登记照片为空或超过5MiB')
    return raw


def prepare_people(people, extractor, decoder, photo_reader=read_photo):
    results = []
    for person in people:
        result = dict(personId=person['personId'], status='FAILED', recognitionReady=False)
        try:
            for field in ('personId', 'personName', 'personIdNum'):
                value = person.get(field)
                if not isinstance(value, str) or not value or len(value.encode('utf-8')) > (128 if field == 'personIdNum' else 1024) or any(ord(c) < 32 for c in value):
                    raise ValueError(field + '必须是非空字符串')
            if person.get('seat') is not None and not isinstance(person['seat'], str):
                raise ValueError('seat必须是字符串或null')
            raw = photo_reader(person.get('personPhoto'))
            digest = hashlib.sha256(raw).hexdigest()
            # The enrollment extractor only requires explicit scope. These local
            # placeholders never escape into activity admission or platform IDs.
            metadata = dict(activity_id='photo-enrollment', activity_num='photo-enrollment',
                            config_version=1, binding_version=1, monitoring_version=1,
                            source_id='platform-photo', camera_id='enrollment', frame_id=digest,
                            time_source='UNKNOWN', purpose='GALLERY_ENROLLMENT')
            detail = extractor.extract_detailed([(decoder(raw), metadata)])[0]
            if detail['detected_faces'] != 1:
                raise ValueError('登记照片必须恰好包含一张人脸')
            if len(detail['observations']) != 1:
                raise ValueError('登记照片人脸质量不合格')
            observation = detail['observations'][0]
            template_id = 'platform-photo:' + hashlib.sha256(
                json.dumps([person['personId'], digest], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            prepared = dict(person, photo_sha256=digest, templates=[dict(
                template_id=template_id, feature_space=extractor.feature_space,
                embedding=observation['embedding'])])
            result.update(status='SUCCESS', recognitionReady=True, person=prepared)
        except ValueError as error:
            # Only known application messages may be returned; URL failures can contain secrets.
            message = str(error)
            safe = ('personId', 'personName', 'personIdNum', 'personPhoto', 'seat', '登记照片')
            result['message'] = message if message.startswith(safe) and 'http' not in message else '登记照片无法读取或解码'
        except Exception:
            result['message'] = '登记照片无法读取或处理'
        results.append(result)
    return results


def capture_base64(image, face_bbox):
    """Encode this observation's face from the original camera frame.

    The detection box belongs to the same face used to compute the embedding.
    Invalid boxes fail rather than substituting another face or a full frame.
    """
    import cv2
    try:
        x1, y1, x2, y2 = [float(value) for value in face_bbox]
    except (TypeError, ValueError):
        raise ValueError('抓拍人脸边界无效') from None
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2)) or x2 <= x1 or y2 <= y1:
        raise ValueError('抓拍人脸边界无效')
    height, width = image.shape[:2]
    left, top = max(0, min(width, math.floor(x1))), max(0, min(height, math.floor(y1)))
    right, bottom = max(0, min(width, math.ceil(x2))), max(0, min(height, math.ceil(y2)))
    if right <= left or bottom <= top:
        raise ValueError('抓拍人脸区域为空')
    frame = image[top:bottom, left:right]
    for _ in range(6):
        for quality in (90, 75, 55):
            ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if ok and len(encoded) <= MAX_PHOTO_BYTES:
                return base64.b64encode(encoded.tobytes()).decode('ascii')
        height, width = frame.shape[:2]
        if min(height, width) < 64:
            break
        frame = cv2.resize(frame, (max(1, width * 3 // 4), max(1, height * 3 // 4)))
    raise ValueError('抓拍照片编码失败或超过5MiB')
