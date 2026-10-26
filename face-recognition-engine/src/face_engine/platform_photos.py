"""Platform v4.1 photo enrollment and bounded capture encoding.

No pictures, credentials, or vectors are printed to logs. Enrollment stdout is
an internal JSON pipe consumed by the algorithm daemon, never by the platform.
"""
import base64
import hashlib
from http.client import HTTPException, InvalidURL
import json
import math
import re
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

MAX_PHOTO_BYTES = 5 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 15
# Failure messages keep the original wording as their prefix; details follow '：'.
INVALID_URL = 'personPhoto必须是完整HTTP(S) URL'
UNREADABLE = '登记照片无法读取或解码'
UNPROCESSABLE = '登记照片无法读取或处理'
_STRAY_PERCENT = re.compile(r'%(?![0-9A-Fa-f]{2})')


def _encode_iri_component(text):
    """RFC 3987 section 3.1: keep ASCII, UTF-8 percent-encode the rest.

    Existing %XX escapes are preserved, so encoded URLs are not double-encoded.
    A '%' that does not start an escape cannot be valid URI text and is sent as %25.
    """
    text = _STRAY_PERCENT.sub('%25', text)
    return ''.join(c if ord(c) < 128 else ''.join('%%%02X' % b for b in c.encode('utf-8')) for c in text)


def normalize_photo_url(url):
    """Return the ASCII request URI for a platform photo URL or IRI.

    Platform file names are often Chinese; http.client only accepts ASCII
    request targets, so non-ASCII path/query text is UTF-8 percent-encoded and
    a non-ASCII host is IDNA-encoded. URL acceptance rules are unchanged.
    """
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) <= 32 for c in url):
        raise ValueError(INVALID_URL)
    try:
        address = urlsplit(url)
    except ValueError:
        raise ValueError(UNREADABLE + '：照片地址无效') from None
    if address.scheme not in ('http', 'https') or not address.hostname or address.username or address.password or address.fragment:
        raise ValueError(INVALID_URL)
    netloc = address.netloc
    if not netloc.isascii():
        # No userinfo is allowed and IPv6 literals are ASCII: netloc is host[:port].
        host, colon, port = netloc.partition(':')
        try:
            netloc = host.encode('idna').decode('ascii') + colon + port
        except UnicodeError:
            raise ValueError(UNREADABLE + '：照片地址的主机名无效') from None
    return urlunsplit((address.scheme, netloc, _encode_iri_component(address.path),
                       _encode_iri_component(address.query), ''))


def _network_failure(error):
    reason = error.reason if isinstance(error, URLError) and isinstance(error.reason, BaseException) else error
    if isinstance(reason, InvalidURL):
        return '地址或响应无效'
    if isinstance(reason, TimeoutError):
        return '连接或读取超时'
    if isinstance(reason, socket.gaierror):
        return '无法解析主机名'
    if isinstance(reason, ConnectionRefusedError):
        return '连接被拒绝'
    if isinstance(reason, ssl.SSLCertVerificationError):
        return 'HTTPS证书校验失败'
    if isinstance(reason, ssl.SSLError):
        return 'TLS握手失败'
    if isinstance(reason, (ConnectionError, HTTPException)):
        return '连接中断或响应不完整'
    return '网络不可达'


def read_photo(url):
    request_url = normalize_photo_url(url)
    try:
        # Platform file servers can be on the private LAN; do not restrict them to public DNS.
        with urlopen(Request(request_url, headers={'Accept': 'image/jpeg, image/png'}), timeout=DOWNLOAD_TIMEOUT_SECONDS) as reply:
            if urlsplit(reply.geturl()).scheme not in ('http', 'https'):
                raise ValueError('登记照片重定向地址无效')
            raw = reply.read(MAX_PHOTO_BYTES + 1)
    except HTTPError as error:
        # Status only: URLs, headers and bodies can carry credentials.
        raise ValueError(UNPROCESSABLE + '：下载失败，服务器返回HTTP %d' % error.code) from None
    except (URLError, HTTPException, OSError) as error:
        raise ValueError(UNPROCESSABLE + '：下载失败，' + _network_failure(error)) from None
    except ValueError as error:
        if str(error).startswith('登记照片'):
            raise
        raise ValueError(UNREADABLE + '：照片地址或响应无效') from None
    if not raw or len(raw) > MAX_PHOTO_BYTES:
        raise ValueError('登记照片为空或超过5MiB')
    return raw


# reason -> (description, metric, threshold(s), detail format); thresholds come from the policy actually applied.
_QUALITY_REASONS = {
    'FACE_TOO_SMALL': ('人脸过小', 'face_size_px', ('min_face_size_px',), '{:.0f}px，要求≥{:.0f}px'),
    'INTER_EYE_DISTANCE_TOO_SMALL': ('双眼间距过小', 'inter_eye_distance_px', ('min_inter_eye_distance_px',), '{:.0f}px，要求≥{:.0f}px'),
    'LOW_DETECTION_SCORE': ('人脸检测置信度低', 'detection_score', ('min_detection_score',), '{:.2f}，要求≥{:.2f}'),
    'BLURRED': ('清晰度不足', 'laplacian_variance', ('min_laplacian_variance',), '拉普拉斯方差{:.0f}，要求≥{:.0f}'),
    'FACE_CLIPPED': ('人脸被图片边缘截断', 'clipped_fraction', ('max_clipped_fraction',), '{:.1%}，上限{:.1%}'),
    'EXTREME_YAW_PROXY': ('侧脸角度过大', 'yaw_proxy', ('max_yaw_proxy',), '{:.2f}，上限{:.2f}'),
    'EXTREME_PITCH_PROXY': ('抬头或低头角度过大', 'pitch_proxy', ('min_pitch_proxy', 'max_pitch_proxy'), '{:.2f}，要求{:.2f}～{:.2f}'),
    'ALIGNMENT_FAILED': ('人脸关键点对齐失败', None, (), ''),
}
_INVALID_GEOMETRY = ('INVALID_DETECTION', 'INVALID_FACE_BOX', 'INVALID_LANDMARKS', 'LANDMARK_OUTSIDE_FACE',
                     'DEGENERATE_LANDMARKS', 'FACE_OUTSIDE_IMAGE')


def quality_failure_message(rejected_faces):
    """Explain a single-face quality rejection with its reason codes and measured values."""
    quality = rejected_faces[0].get('quality') if isinstance(rejected_faces, list) and rejected_faces and isinstance(rejected_faces[0], dict) else None
    reasons = quality.get('reasons') if isinstance(quality, dict) else None
    if not isinstance(reasons, list) or not reasons:
        return '登记照片人脸质量不合格'
    metrics = quality.get('metrics') if isinstance(quality.get('metrics'), dict) else {}
    thresholds = quality.get('thresholds') if isinstance(quality.get('thresholds'), dict) else {}
    parts = []
    for reason in reasons[:8]:
        if not isinstance(reason, str) or not re.fullmatch(r'[A-Z_]{1,64}', reason):
            continue
        description, metric, limits, detail = _QUALITY_REASONS.get(reason, (
            '人脸框或关键点无效' if reason in _INVALID_GEOMETRY else '', None, (), ''))
        values = [metrics.get(metric)] + [thresholds.get(name) for name in limits] if metric else []
        if values and all(type(v) in (int, float) and math.isfinite(v) for v in values):
            description += '（' + detail.format(*values) + '）'
        parts.append((reason + ' ' + description).strip())
    return '登记照片人脸质量不合格：' + '；'.join(parts) if parts else '登记照片人脸质量不合格'


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
            # Each stage keeps the message its exception type produced before.
            try:
                image = decoder(raw)
            except ValueError:
                raise ValueError(UNREADABLE + '：仅支持有效的JPEG/PNG图片，且像素数不能超过上限') from None
            except Exception:
                raise ValueError(UNPROCESSABLE + '：图片解码失败') from None
            try:
                detail = extractor.extract_detailed([(image, metadata)])[0]
            except ValueError:
                raise ValueError(UNREADABLE + '：人脸检测或特征提取失败') from None
            except Exception:
                raise ValueError(UNPROCESSABLE + '：人脸检测或特征提取失败') from None
            if detail['detected_faces'] != 1:
                count = detail['detected_faces']
                raise ValueError('登记照片必须恰好包含一张人脸：' + (
                    '未检测到人脸，请使用正面免冠照片，人脸周围保留背景，不要裁到只剩人脸' if count == 0
                    else '检测到%d张人脸，请使用单人照片' % count))
            if len(detail['observations']) != 1:
                raise ValueError(quality_failure_message(detail.get('rejected_faces')))
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
