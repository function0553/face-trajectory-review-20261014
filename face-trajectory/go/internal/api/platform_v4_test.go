package api

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"face-trajectory/internal/push"
	"face-trajectory/internal/trust"
	"face-trajectory/internal/worker"
	"image"
	"image/jpeg"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func syntheticSpace() map[string]any {
	return map[string]any{"recognition_model_sha256": strings.Repeat("a", 64), "detector_model_sha256": strings.Repeat("b", 64), "detector_profile": "SYNTHETIC", "alignment_version": "SYNTHETIC", "preprocessing_version": "SYNTHETIC", "output_version": "SYNTHETIC-512"}
}

type syntheticPhotos struct{ calls int }
type unavailablePhotos struct{}

func (unavailablePhotos) Prepare(context.Context, []map[string]any) ([]map[string]any, error) {
	return nil, errors.New("photo importer unavailable")
}

func TestV41SyncReportsSavedActivityWhenImporterFails(t *testing.T) {
	f := fixture(t)
	f.handler = newAuthorized(f.worker, &trust.Verifier{Keys: trust.Keys{"test": f.key.Public().(ed25519.PublicKey)}, Issuer: f.claims.Issuer}, f.clock, unavailablePhotos{})
	status, reply := f.request(t, "/v1/activities/sync", map[string]any{"activityId": "A", "activityName": "SYNTHETIC", "activityNum": "NUM-A", "activityState": "0", "activityStart": "2026-10-01T00:00:00Z", "activityEnd": "2026-10-30T00:00:00Z", "keyPersons": []any{map[string]any{"personId": "P"}}}, &f.claims)
	if status != 503 {
		t.Fatal("unavailable importer was acknowledged as ready")
	}
	data := reply["data"].(map[string]any)
	if data["activitySaved"] != true || data["failedCount"] != float64(1) || len(f.worker.calls) != 1 {
		t.Fatal("saved activity and failed import were not distinguished")
	}
}

func (p *syntheticPhotos) Prepare(_ context.Context, people []map[string]any) ([]map[string]any, error) {
	p.calls++
	results := []map[string]any{}
	for _, person := range people {
		r := map[string]any{"personId": person["personId"], "status": "FAILED", "message": "照片中未检测到人脸"}
		if person["personId"] != "BAD" {
			embedding := make([]float64, 512)
			embedding[0] = 1
			copy := map[string]any{}
			for k, v := range person {
				copy[k] = v
			}
			copy["photo_sha256"] = "SYNTHETIC"
			copy["templates"] = []any{map[string]any{"template_id": "photo:" + person["personId"].(string), "feature_space": syntheticSpace(), "embedding": embedding}}
			r["status"] = "SUCCESS"
			r["person"] = copy
		}
		results = append(results, r)
	}
	return results, nil
}
func TestV41BodyContractAndScope(t *testing.T) {
	f := fixture(t)
	for _, path := range []string{"/v1/activities/bindings", "/v1/monitoring/persons"} {
		key := "cameras"
		if strings.Contains(path, "persons") {
			key = "keyPersons"
		}
		status, _ := f.request(t, path, map[string]any{key: []any{}}, &f.claims)
		if status != 400 || len(f.worker.calls) != 0 {
			t.Fatal("missing Body activityId reached worker")
		}
		status, _ = f.request(t, path+"?activityId=A", map[string]any{key: []any{}}, &f.claims)
		if status != 400 || len(f.worker.calls) != 0 {
			t.Fatal("v4.0 query association was accepted")
		}
		status, _ = f.request(t, path, map[string]any{"activityId": "B", key: []any{}}, &f.claims)
		if status != 403 || len(f.worker.calls) != 0 {
			t.Fatal("cross-activity action reached worker")
		}
	}
	status, _ := f.request(t, "/v1/activities/bindings", map[string]any{"activityId": "A", "cameras": []any{}, "binding_version": 1}, &f.claims)
	if status != 400 {
		t.Fatal("mixed old CAS protocol accepted")
	}
	status, _ = f.request(t, "/v1/activities/monitoring", map[string]any{"activityId": "A", "enabled": nil}, &f.claims)
	if status != 400 {
		t.Fatal("null monitoring switch accepted")
	}
	status, _ = f.request(t, "/v1/monitoring/persons", map[string]any{"activityId": "A", "keyPersons": nil}, &f.claims)
	if status != 400 {
		t.Fatal("null personnel list accepted")
	}
}

func TestV41RealWorkerHTTPAndPush(t *testing.T) {
	executable := os.Getenv("FACE_TRAJECTORY_TEST_WORKER")
	if executable == "" {
		t.Skip("set FACE_TRAJECTORY_TEST_WORKER for real worker integration")
	}
	client, err := worker.StartConfiguredPush(executable, filepath.Join(t.TempDir(), "state.sqlite"), 32, 64, 100, true)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	f := fixture(t)
	clocked := &trust.Clocked{Worker: client, Clock: f.clock}
	photos := &syntheticPhotos{}
	handler := newAuthorized(clocked, &trust.Verifier{Keys: trust.Keys{"test": f.key.Public().(ed25519.PublicKey)}, Issuer: f.claims.Issuer}, f.clock, photos)
	f.handler = handler
	call := func(value map[string]any) map[string]any {
		raw, _ := json.Marshal(value)
		body, e := clocked.Request(context.Background(), raw)
		if e != nil {
			t.Fatal(e)
		}
		var result map[string]any
		if json.Unmarshal(body, &result) != nil || result["ok"] != true {
			t.Fatalf("worker %s: %s", value["op"], body)
		}
		return result
	}
	cameras := []any{map[string]any{"deviceId": "UUID-1", "deviceName": "后端入口1"}, map[string]any{"deviceId": "UUID-2", "deviceName": "后端入口2"}}
	person := map[string]any{"personId": "P", "personName": "模拟人员", "personIdNum": "SYNTHETIC-ID-001", "personPhoto": "https://photos.example.test/profile.jpg", "seat": nil}
	activity := map[string]any{"activityId": "A", "activityName": "SYNTHETIC", "activityNum": "NUM-A", "activityState": "0", "activityStart": "2026-10-10T00:00:00Z", "activityEnd": "2026-10-20T00:00:00Z", "cameras": cameras, "keyPersons": []any{person, map[string]any{"personId": "BAD"}}}
	// The fixture timestamp is 2026-10-10. Keep the window around it explicitly.
	activity["activityStart"] = time.UnixMilli(fixtureUTC - 86400000).UTC().Format(time.RFC3339)
	activity["activityEnd"] = time.UnixMilli(fixtureUTC + 86400000).UTC().Format(time.RFC3339)
	status, result := f.request(t, "/v1/activities/sync", activity, &f.claims)
	if status != 200 {
		t.Fatalf("sync status %d: %v", status, result)
	}
	data := result["data"].(map[string]any)
	if data["successCount"] != float64(1) || data["failedCount"] != float64(1) || data["activitySaved"] != true {
		t.Fatal("partial import was misreported")
	}
	for _, path := range []string{"/v1/activities/bindings", "/v1/monitoring/persons"} {
		body := map[string]any{"activityId": "A", "cameras": cameras}
		if strings.Contains(path, "persons") {
			body = map[string]any{"activityId": "A", "keyPersons": []any{person}}
		}
		code, r := f.request(t, path, body, &f.claims)
		if code != 200 {
			t.Fatalf("%s: %d %v", path, code, r)
		}
	}
	status, _ = f.request(t, "/v1/activities/monitoring", map[string]any{"activityId": "A", "enabled": true}, &f.claims)
	if status != 200 {
		t.Fatal("enable failed")
	}
	for _, camera := range []string{"UUID-1", "UUID-2"} {
		call(map[string]any{"op": "clock_profile_put", "profile": map[string]any{"camera_id": camera, "profile_version": "good", "verified": true, "capture_semantics": "DEVICE_CAPTURE", "source_evidence_id": "SYNTHETIC", "fixed_utc_offset_minutes": 0, "calibrations": []any{map[string]any{"calibration_id": "SYNTHETIC", "source_evidence_id": "SYNTHETIC", "valid_from_utc_ms": fixtureUTC - 86400000, "valid_until_utc_ms": fixtureUTC + 86400000, "offset_ms": 0, "uncertainty_ms": 1, "time_resolution_ms": 1}}}})
	}
	var photo bytes.Buffer
	_ = jpeg.Encode(&photo, image.NewRGBA(image.Rect(0, 0, 32, 32)), nil)
	encoded := base64.StdEncoding.EncodeToString(photo.Bytes())
	embedding := make([]float64, 512)
	embedding[0] = 1
	policy := map[string]any{"policy_id": "SYNTHETIC", "match_threshold": .85, "new_target_threshold": .25, "min_margin": .1, "template_update_threshold": .9, "max_template_similarity": .999, "min_detection_score": .5, "min_face_size_px": 32, "require_topology": false, "topology_is_complete": false}
	for i, camera := range []string{"UUID-1", "UUID-2"} {
		body := map[string]any{"activity_id": "A", "activity_num": "NUM-A", "config_version": 1, "binding_version": 1, "monitoring_version": 1, "route_rule_id": "LOCAL-OLD-ROUTE", "observation": map[string]any{"observation_id": "face-" + camera, "source_id": "LOCAL-SOURCE", "camera_id": camera, "camera_name": "设备旧名", "frame_id": "frame-" + camera, "feature_space": syntheticSpace(), "embedding": embedding, "detection_score": .99, "face_size_px": 64, "capture_photo_base64": encoded}, "time_record": map[string]any{"profile_version": "good", "raw_time": time.UnixMilli(fixtureUTC + int64(i)*1000).UTC().Format(time.RFC3339Nano)}, "policy": policy}
		body["time_record"].(map[string]any)["source_id"] = "LOCAL-SOURCE"
		code, r := f.request(t, "/v1/monitoring/observe", body, &f.claims)
		if code != 200 || r["status"] != "ACCEPTED" {
			t.Fatalf("observation failed: %d %v", code, r)
		}
	}
	head := call(map[string]any{"op": "push_next"})["event"].(map[string]any)
	event := head["person_track_event"].(map[string]any)
	if event["person_name"] != "模拟人员" || event["person_id_num"] != "SYNTHETIC-ID-001" || event["device_id"] != "UUID-1" || event["camera_name"] != "后端入口1" || event["capture_photo_url"] != encoded || event["source_id"] != nil {
		t.Fatal("v4.1 outbound fields mismatch")
	}
	received := make(chan []byte, 4)
	receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body bytes.Buffer
		_, _ = body.ReadFrom(r.Body)
		received <- append([]byte(nil), body.Bytes()...)
		_ = json.NewEncoder(w).Encode(map[string]any{"code": 200, "event_id": event["event_id"]})
	}))
	defer receiver.Close()
	sender, e := push.New(clocked, receiver.URL, "Bearer SYNTHETIC", f.clock)
	if e != nil {
		t.Fatal(e)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { defer close(done); sender.Run(ctx) }()
	select {
	case body := <-received:
		var wire map[string]any
		_ = json.Unmarshal(body, &wire)
		if len(wire) != 14 || wire["capture_photo_url"] != encoded {
			t.Fatal("push wire contract mismatch")
		}
	case <-time.After(8 * time.Second):
		t.Fatal("push not received")
	}
	deadline := time.Now().Add(3 * time.Second)
	for call(map[string]any{"op": "push_next"})["event"] != nil {
		if time.Now().After(deadline) {
			t.Fatal("cross-camera five-second dedup or acknowledgement failed")
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	<-done
	if photos.calls != 2 {
		t.Fatal("sync and explicit import did not share photo preparation")
	}
}
