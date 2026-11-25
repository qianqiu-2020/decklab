// Query an unconfirmed edge submission with the standard library; never resubmit.
// Usage: DECKLAB_URL=... DECKLAB_TOKEN=... go run examples/edge_task_lookup.go REQUEST_ID
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

func get(client *http.Client, base, token, path string, value any) error {
	req, err := http.NewRequest(http.MethodGet, base+path, nil)
	if err != nil {
		return err
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := client.Do(req)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("GET %s: HTTP %d; original submission remains unconfirmed", req.URL.Path, response.StatusCode)
	}
	return json.NewDecoder(io.LimitReader(response.Body, 2<<20)).Decode(value)
}

func run(requestID string) error {
	base := strings.TrimRight(os.Getenv("DECKLAB_URL"), "/")
	if base == "" {
		base = "http://127.0.0.1:8898"
	}
	endpoint, err := url.Parse(base)
	if err != nil || endpoint.Host == "" || endpoint.User != nil || endpoint.RawQuery != "" || endpoint.Fragment != "" || (endpoint.Scheme != "http" && endpoint.Scheme != "https") {
		return errors.New("DECKLAB_URL must be an HTTP(S) service URL without credentials/query/fragment")
	}
	// A redirect is not part of this fixed control URL's contract. In particular,
	// do not redirect credentials or add an application-level retry around writes.
	client := &http.Client{Timeout: 10 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	token := os.Getenv("DECKLAB_TOKEN")
	var capability struct {
		APIVersion int `json:"api_version"`
		Contract   struct {
			Name     string `json:"name"`
			Revision int    `json:"revision"`
		} `json:"contract"`
		Tasks struct {
			Lookup struct {
				Supported bool   `json:"supported"`
				Endpoint  string `json:"endpoint"`
				Parameter string `json:"parameter"`
			} `json:"request_lookup"`
		} `json:"tasks"`
	}
	if err := get(client, base, token, "/api/capabilities", &capability); err != nil {
		return err
	}
	if capability.APIVersion != 1 || capability.Contract.Name != "decklab-execution" || capability.Contract.Revision < 1 || !capability.Tasks.Lookup.Supported || capability.Tasks.Lookup.Endpoint != "/api/task" || capability.Tasks.Lookup.Parameter != "request_id" {
		return errors.New("server lacks the original-request lookup contract")
	}
	var result struct {
		Task struct {
			ID        string `json:"id"`
			RequestID string `json:"request_id"`
			State     string `json:"state"`
			Outcome   string `json:"outcome"`
		} `json:"task"`
	}
	actor := os.Getenv("DECKLAB_ACTOR")
	if actor == "" {
		actor = "agent"
	}
	// Owner credentials may select their original POST actor. An Agent grant's
	// actor remains pinned by control regardless of this query value.
	if err := get(client, base, token, "/api/task?request_id="+url.QueryEscape(requestID)+"&lookup_actor="+url.QueryEscape(actor), &result); err != nil {
		return err
	}
	if result.Task.ID == "" || result.Task.RequestID != requestID {
		return errors.New("response does not identify the original request")
	}
	switch result.Task.Outcome {
	case "not-issued", "issued", "acknowledged", "observed", "verified", "unknown":
	default:
		result.Task.Outcome = "unknown"
	}
	// completed is a lifecycle state; content verification and test acceptance
	// still need their own evidence. This helper performs GET requests only.
	return json.NewEncoder(os.Stdout).Encode(result)
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: edge_task_lookup REQUEST_ID")
		os.Exit(2)
	}
	if err := run(os.Args[1]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
