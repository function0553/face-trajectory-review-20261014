package api

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"image"
	_ "image/jpeg"
	_ "image/png"
	"io"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"time"
)

// The helper is algorithm-owned. Public platform requests never carry embeddings.
type PhotoPreparer interface {
	Prepare(context.Context, []map[string]any) ([]map[string]any, error)
}
type execPhotoPreparer struct{ python, script, config string }
type boundedOutput struct {
	bytes.Buffer
	limit int
}

func (b *boundedOutput) Write(p []byte) (int, error) {
	if len(p) > b.limit-b.Len() {
		return 0, errors.New("photo helper output exceeds limit")
	}
	return b.Buffer.Write(p)
}
func (p *execPhotoPreparer) Prepare(ctx context.Context, people []map[string]any) ([]map[string]any, error) {
	if p.python == "" || p.script == "" || p.config == "" {
		return nil, errors.New("algorithm photo importer is not configured")
	}
	input, _ := json.Marshal(map[string]any{"keyPersons": people})
	cmd := exec.CommandContext(ctx, p.python, p.script, "--model-config", p.config)
	cmd.Env = append(os.Environ(), "PYTHONIOENCODING=utf-8")
	cmd.Stdin = bytes.NewReader(input)
	cmd.Stderr = io.Discard
	output := &boundedOutput{limit: 16 * 1024 * 1024}
	cmd.Stdout = output
	if err := cmd.Run(); err != nil {
		return nil, errors.New("algorithm photo importer unavailable or timed out")
	}
	var result struct {
		Persons []map[string]any `json:"keyPersons"`
	}
	if json.Unmarshal(output.Bytes(), &result) != nil || len(result.Persons) != len(people) {
		return nil, errors.New("invalid photo importer result")
	}
	for i, p := range result.Persons {
		if p["personId"] != people[i]["personId"] {
			return nil, errors.New("photo importer person mismatch")
		}
	}
	return result.Persons, nil
}
func configuredPreparer() PhotoPreparer {
	return &execPhotoPreparer{os.Getenv("FACE_TRAJECTORY_P1_PYTHON"), os.Getenv("FACE_TRAJECTORY_PHOTO_IMPORT_SCRIPT"), os.Getenv("FACE_TRAJECTORY_P1_MODEL_CONFIG")}
}
func onlyFields(body map[string]json.RawMessage, fields ...string) bool {
	allowed := map[string]bool{}
	for _, f := range fields {
		allowed[f] = true
	}
	for f := range body {
		if !allowed[f] {
			return false
		}
	}
	return true
}
func stringField(body map[string]json.RawMessage, key string) string {
	var s string
	_ = json.Unmarshal(body[key], &s)
	return s
}
func platformError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"code": status, "message": message})
}
func (s *Server) platformV4(w http.ResponseWriter, r *http.Request, body map[string]json.RawMessage, op, action string) bool {
	// Existing snake_case clients retain their complete old contract.
	v4 := body["activityId"] != nil || body["cameras"] != nil || body["keyPersons"] != nil || r.URL.Query().Has("activityId")
	if !v4 {
		return false
	}
	if op != "activity_sync" && op != "activity_bindings" && op != "activity_monitoring" && op != "monitoring_person_put" {
		platformError(w, 400, "v4 parameters are not supported on this endpoint")
		return true
	}
	claims, _, ok := s.authorize(w, r)
	if !ok {
		return true
	}
	id := stringField(body, "activityId")
	if r.URL.Query().Has("activityId") {
		platformError(w, 400, "v4.1 requires activityId in JSON Body, without an activity query parameter")
		return true
	}
	if id == "" || len(id) > 1024 {
		platformError(w, 400, "explicit activityId is required")
		return true
	}
	if !claims.Unrestricted && (!claims.Allows(id, action) || (len(body["keyPersons"]) > 0 && string(body["keyPersons"]) != "[]" && !claims.Allows(id, "monitor"))) {
		platformError(w, 403, "activity action is not authorized")
		return true
	}
	internal := map[string]json.RawMessage{"activity_id": rawString(id)}
	invoke := func(operation string, extra map[string]any) (map[string]json.RawMessage, int, error) {
		request := map[string]json.RawMessage{}
		for k, v := range internal {
			request[k] = v
		}
		request["op"] = rawString(operation)
		request["_client_context"], _ = json.Marshal(claims.ClientContext)
		request["_authorization_validity"], _ = json.Marshal(map[string]int64{"not_before_unix_ms": claims.NotBefore, "expires_unix_ms": claims.Expires})
		for k, v := range extra {
			request[k], _ = json.Marshal(v)
		}
		return s.exchange(r.Context(), request)
	}
	allowed := false
	switch op {
	case "activity_sync":
		allowed = onlyFields(body, "activityId", "activityName", "activityNum", "activityState", "activityStart", "activityEnd", "cameras", "keyPersons")
	case "activity_bindings":
		allowed = onlyFields(body, "activityId", "cameras") && body["cameras"] != nil
	case "activity_monitoring":
		allowed = onlyFields(body, "activityId", "enabled") && body["enabled"] != nil
	case "monitoring_person_put":
		allowed = onlyFields(body, "activityId", "keyPersons") && body["keyPersons"] != nil
	}
	if !allowed {
		platformError(w, 400, "unexpected or missing v4 fields; old CAS and template fields are not accepted")
		return true
	}
	var normalized map[string]any
	_ = json.Unmarshal(mustJSON(body), &normalized)
	if op == "activity_sync" {
		for _, field := range []string{"activityId", "activityName", "activityNum", "activityState", "activityStart", "activityEnd"} {
			if strings.TrimSpace(stringField(body, field)) == "" {
				platformError(w, 400, "missing activity field: "+field)
				return true
			}
		}
		start, e1 := time.Parse(time.RFC3339Nano, stringField(body, "activityStart"))
		end, e2 := time.Parse(time.RFC3339Nano, stringField(body, "activityEnd"))
		if e1 != nil || e2 != nil || !end.After(start) {
			platformError(w, 400, "activityStart/activityEnd must be ISO8601 with timezone and a positive interval")
			return true
		}
		normalized["start_unix_ms"] = start.UnixMilli()
		normalized["end_unix_ms"] = end.UnixMilli()
	}
	if raw := body["cameras"]; raw != nil {
		var cameras []map[string]json.RawMessage
		if json.Unmarshal(raw, &cameras) != nil || string(raw) == "null" || len(cameras) > 10000 {
			platformError(w, 400, "invalid cameras array")
			return true
		}
		seen := map[string]bool{}
		for _, camera := range cameras {
			device := stringField(camera, "deviceId")
			if !onlyFields(camera, "deviceId", "deviceName") || device == "" || len(device) > 1024 || strings.TrimSpace(stringField(camera, "deviceName")) == "" || seen[device] {
				platformError(w, 400, "invalid or duplicate camera")
				return true
			}
			seen[device] = true
		}
	}
	if op == "activity_monitoring" {
		var enabled bool
		if json.Unmarshal(body["enabled"], &enabled) != nil || string(body["enabled"]) == "null" {
			platformError(w, 400, "enabled must be boolean")
			return true
		}
	}
	var people []map[string]any
	if raw := body["keyPersons"]; raw != nil {
		var entries []map[string]json.RawMessage
		if json.Unmarshal(raw, &entries) != nil || string(raw) == "null" || len(entries) > 500 {
			platformError(w, 400, "keyPersons must contain at most 500 people")
			return true
		}
		seen := map[string]bool{}
		for _, person := range entries {
			if !onlyFields(person, "personId", "personName", "personIdNum", "personPhoto", "seat") {
				platformError(w, 400, "unexpected person fields")
				return true
			}
			pid := stringField(person, "personId")
			if pid == "" || len(pid) > 1024 || seen[pid] {
				platformError(w, 400, "invalid or duplicate personId")
				return true
			}
			seen[pid] = true
			var value map[string]any
			_ = json.Unmarshal(mustJSON(person), &value)
			people = append(people, value)
		}
	}
	var reply map[string]json.RawMessage
	var status int
	var err error
	operation := map[string]string{"activity_sync": "platform_v4_sync", "activity_bindings": "platform_v4_bindings", "activity_monitoring": "platform_v4_monitoring", "monitoring_person_put": "platform_v4_context"}[op]
	reply, status, err = invoke(operation, map[string]any{"body": normalized})
	if err != nil {
		platformError(w, status, err.Error())
		return true
	}
	if status != 200 {
		message := stringField(reply, "error")
		if strings.HasPrefix(message, "UNKNOWN_ACTIVITY:") {
			status = 404
		}
		platformError(w, status, message)
		return true
	}
	if op == "monitoring_person_put" || body["keyPersons"] != nil {
		results := make([]map[string]any, 0, len(people))
		success := 0
		failBatch := func(message string) {
			failed := make([]map[string]any, 0, len(people))
			for _, person := range people {
				failed = append(failed, map[string]any{"personId": person["personId"], "status": "FAILED", "recognitionReady": false, "message": message})
			}
			writeReply(w, 503, map[string]json.RawMessage{"code": json.RawMessage("503"), "message": rawString(message), "activityId": rawString(id), "data": mustJSON(map[string]any{"total": len(people), "successCount": 0, "failedCount": len(people), "keyPersons": failed, "activitySaved": op == "activity_sync"})})
		}
		if len(people) > 0 {
			select {
			case s.photoGate <- struct{}{}:
			default:
				failBatch("photo importer is busy; retry the same request")
				return true
			}
			ctx, cancel := context.WithTimeout(r.Context(), 5*time.Minute)
			prepared, prepErr := s.preparer.Prepare(ctx, people)
			cancel()
			<-s.photoGate
			if prepErr != nil {
				failBatch(prepErr.Error())
				return true
			}
			// Import can outlive the original identity or UTC proof. Reauthorize before committing.
			refreshed, _, authorized := s.authorize(w, r)
			if !authorized {
				return true
			}
			claims = refreshed
			if !claims.Unrestricted && !claims.Allows(id, "monitor") {
				platformError(w, 403, "person import is no longer authorized")
				return true
			}
			for _, person := range prepared {
				result := map[string]any{"personId": person["personId"], "status": "FAILED", "recognitionReady": false, "message": "人员模板未就绪"}
				if person["status"] != "SUCCESS" {
					result["message"] = person["message"]
					results = append(results, result)
					continue
				}
				registered, code, e := invoke("platform_v4_person", map[string]any{"person": person["person"]})
				if e != nil {
					result["message"] = e.Error()
				} else if code != 200 {
					result["message"] = stringField(registered, "error")
				} else {
					var ready bool
					_ = json.Unmarshal(registered["recognition_ready"], &ready)
					if ready {
						result["status"] = "SUCCESS"
						result["recognitionReady"] = true
						result["message"] = "入库完成"
						success++
						var expiry int64
						_ = json.Unmarshal(registered["expires_at_unix_ms"], &expiry)
						result["expiresAtUnixMs"] = expiry
					}
				}
				results = append(results, result)
			}
		}
		writeReply(w, 200, map[string]json.RawMessage{"code": json.RawMessage("200"), "message": rawString("本次处理完成"), "activityId": rawString(id), "data": mustJSON(map[string]any{"total": len(people), "successCount": success, "failedCount": len(people) - success, "keyPersons": results, "activitySaved": op == "activity_sync"})})
		return true
	}
	delete(reply, "ok")
	writeReply(w, status, reply)
	return true
}
func mustJSON(value any) json.RawMessage { body, _ := json.Marshal(value); return body }

func validateCapture(body map[string]json.RawMessage) error {
	var observation map[string]json.RawMessage
	if json.Unmarshal(body["observation"], &observation) != nil {
		return nil
	}
	field := observation["capture_photo_base64"]
	if field == nil {
		return nil
	}
	var encoded string
	if json.Unmarshal(field, &encoded) != nil || len(encoded) > 4*((5*1024*1024+2)/3) {
		return errors.New("invalid capture photo Base64 or image exceeds 5MiB")
	}
	raw, err := base64.StdEncoding.Strict().DecodeString(encoded)
	if err != nil || len(raw) > 5*1024*1024 {
		return errors.New("capture photo must be pure Base64 <=5MiB")
	}
	config, format, err := image.DecodeConfig(bytes.NewReader(raw))
	if err != nil || (format != "jpeg" && format != "png") || config.Width < 1 || config.Height < 1 || int64(config.Width)*int64(config.Height) > 16000000 {
		return errors.New("invalid JPEG/PNG capture or image exceeds 16 million pixels")
	}
	if _, _, err = image.Decode(bytes.NewReader(raw)); err != nil {
		return errors.New("capture photo cannot be decoded")
	}
	return nil
}
